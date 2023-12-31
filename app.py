import argparse
from typing import Union

# mostly likely you'll need these modules/classes
from clams import ClamsApp, Restifier
from mmif import Mmif, View, Annotation, Document, AnnotationTypes, DocumentTypes

from config import config
import json

import cv2
import torch
import faiss
from transformers import CLIPProcessor, CLIPModel


class Clip(ClamsApp):

    def __init__(self):
        super().__init__()
        index_filepath = "chyron_full.faiss"
        index_map_filepath = "chyron_full.json"
        # load the index
        self.index = faiss.read_index(index_filepath)
        self.index_map = json.load(open(index_map_filepath, "r"))

        self.model = CLIPModel.from_pretrained(config["model_name"])
        self.processor = CLIPProcessor.from_pretrained(config["model_name"])

    def _appmetadata(self):
        # see https://sdk.clams.ai/autodoc/clams.app.html#clams.app.ClamsApp._load_appmetadata
        # Also check out ``metadata.py`` in this directory. 
        # When using the ``metadata.py`` leave this do-nothing "pass" method here. 
        pass

    def get_label(self, frames, threshold):
        # process the frame with the CLIP model
        with torch.no_grad():
            images = self.processor(images=frames, return_tensors="pt")
            image_features = self.model.get_image_features(images["pixel_values"])

        # Convert to numpy array
        image_features_np = image_features.detach().cpu().numpy()
        # calculate cosine similarity
        faiss.normalize_L2(image_features_np)
        D, I = self.index.search(image_features_np, 10)  

        labels_scores = []
        for d_row, i_row in zip(D, I):
            row_labels_scores = []
            for d, i in zip(d_row, i_row):
                if d > threshold:
                    row_labels_scores.append((self.index_map[str(i)], d))
                else:
                    row_labels_scores.append((None, None))
            labels_scores.append(row_labels_scores)
        return labels_scores

    def run_targetdetection(self, video_filename, **kwargs):
        sample_ratio = int(kwargs.get("sampleRatio", 10))
        min_duration = int(kwargs.get("minFrameCount", 10))
        threshold = float(kwargs["threshold"])
        batch_size = 10
        cutoff_minutes = 10

        cap = cv2.VideoCapture(video_filename)
        counter = 0
        rich_timeframes = []
        active_targets = {}  # keys are labels, values are dicts with "start_frame", "start_seconds", "target_scores"
        while True:
            if counter > 30 * 60 * cutoff_minutes:  # Stop processing after cutoff
                break
            frames = []
            frames_counter = []
            for _ in range(batch_size*sample_ratio):
                ret, frame = cap.read()
                if not ret:
                    break
                if counter % sample_ratio == 0:
                    frames.append(frame)
                    frames_counter.append(counter)
                counter += 1
            if not frames:
                break


            labels_scores = self.get_label(frames, threshold)
            for labels_scores_frame, frame_counter in zip(labels_scores, frames_counter):
                for detected_label, score in labels_scores_frame:
                    if detected_label is not None:  # has any label
                        if detected_label not in active_targets:
                            active_targets[detected_label] = {
                                "start_frame": frame_counter,
                                "start_seconds": cap.get(cv2.CAP_PROP_POS_MSEC),
                                "target_scores": [score],
                            }
                        else:
                            active_targets[detected_label]["target_scores"].append(score)
                    else:
                        # process and reset all active_targets
                        for active_label, target_info in active_targets.items():
                            avg_score = sum(target_info["target_scores"]) / len(target_info["target_scores"])
                            if frame_counter - target_info["start_frame"] > min_duration:
                                rich_timeframes.append(
                                    {
                                        "start_frame": target_info["start_frame"],
                                        "end_frame": frame_counter,
                                        "start_seconds": target_info["start_seconds"],
                                        "end_seconds": cap.get(cv2.CAP_PROP_POS_MSEC),
                                        "label": active_label,
                                        "score": float(avg_score),
                                    }
                                )
                        active_targets = {}  # reset active targets

                # process any remaining active_targets at the end
            if active_targets:
                for active_label, target_info in active_targets.items():
                    avg_score = sum(target_info["target_scores"]) / len(target_info["target_scores"])
                    rich_timeframes.append({
                        "start_frame": target_info["start_frame"],
                        "end_frame": counter,
                        "start_seconds": target_info["start_seconds"],
                        "end_seconds": cap.get(cv2.CAP_PROP_POS_MSEC),
                        "label": active_label,
                        "score": float(avg_score),
                    })
        return rich_timeframes

    def _annotate(self, mmif: Union[str, dict, Mmif], **kwargs) -> Mmif:
        # load file location from mmif
        video_filename = mmif.get_document_location(DocumentTypes.VideoDocument)
        config = self.get_configuration(**kwargs)
        unit = config["timeUnit"]
        new_view: View = mmif.new_view()
        self.sign_view(new_view, config)
        new_view.new_contain(
            AnnotationTypes.TimeFrame,
            timeUnit=unit,
            document=mmif.get_documents_by_type(DocumentTypes.VideoDocument)[0].id,
        )
        timeframe_list = self.run_targetdetection(video_filename, **kwargs)
        # add all the timeframes as annotations to the new view
        for timeframe in timeframe_list:
            # skip timeframes that are labeled as "None"
            if timeframe["label"] == "None":
                continue
            timeframe_annotation = new_view.new_annotation(AnnotationTypes.TimeFrame)

            # if the timeUnit is milliseconds, convert the start and end seconds to milliseconds
            if unit == "milliseconds":
                timeframe_annotation.add_property("start", timeframe["start_seconds"])
                timeframe_annotation.add_property("end", timeframe["end_seconds"])
            # otherwise use the frame number
            else:
                timeframe_annotation.add_property("start", timeframe["start_frame"])
                timeframe_annotation.add_property("end", timeframe["end_frame"])
            timeframe_annotation.add_property("frameType", timeframe["label"]),
            timeframe_annotation.add_property("score", timeframe["score"])
        return mmif


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--port", action="store", default="5000", help="set port to listen"
    )
    parser.add_argument("--production", action="store_true", help="run gunicorn server")
    parsed_args = parser.parse_args()

    app = Clip()
    http_app = Restifier(app, port=int(parsed_args.port))
    if parsed_args.production:
        http_app.serve_production()
    else:
        http_app.run()
