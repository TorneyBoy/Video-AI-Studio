"""Bounded colour-change detector using actual decoded presentation timestamps.

Initial profile detects abrupt cuts. It does not claim semantic segmentation,
face/emotion scores, or comprehensive detection of gradual dissolves.
"""

import math
import time


DETECTOR_VERSION = "color-change-pts/1"


def detect_shots(path, *, expected_duration, deadline):
    import av
    import numpy as np

    previous_image, previous_histogram, first_pts, previous_pts = None, None, None, None
    cuts, count = [0.0], 0
    with av.open(str(path), mode="r", options={"protocol_whitelist": "file"}) as container:
        if container.format.name not in {"mov,mp4,m4a,3gp,3g2,mj2", "matroska,webm"} or not container.streams.video:
            raise ValueError("This trial detector accepts MP4/MOV/MKV/WebM video")
        stream = container.streams.video[0]
        if not stream.width or not stream.height or min(stream.width, stream.height) > 1080 or max(stream.width, stream.height) > 1920:
            raise ValueError("Source resolution exceeds the trial detector profile")
        stream.thread_type, stream.codec_context.thread_count = "SLICE", 1
        reported_duration = float(container.duration / av.time_base) if container.duration else None
        if reported_duration is None or not math.isfinite(reported_duration) or abs(reported_duration - expected_duration) > 0.1:
            raise ValueError("Decoded media duration differs from cloud metadata")
        for frame in container.decode(stream):
            count += 1
            if count > 3600 or time.monotonic() > deadline or frame.pts is None or frame.time_base is None:
                raise ValueError("Frame analysis exceeded its limits or has no presentation clock")
            timestamp = float(frame.pts * frame.time_base)
            if not math.isfinite(timestamp) or previous_pts is not None and timestamp < previous_pts:
                raise ValueError("Invalid presentation timestamp sequence")
            first_pts = timestamp if first_pts is None else first_pts
            position, previous_pts = timestamp - first_pts, timestamp
            if position > expected_duration + 0.1:
                raise ValueError("Decoded frames exceed source duration")
            image = frame.reformat(width=160, height=90, format="rgb24").to_ndarray().astype(np.int16)
            histogram = np.concatenate([np.histogram(image[:, :, channel], bins=16, range=(0, 256))[0]
                                        for channel in range(3)]).astype(np.float64)
            histogram /= histogram.sum()
            if previous_image is not None:
                difference = float(np.abs(image - previous_image).mean())
                histogram_change = float(np.abs(histogram - previous_histogram).sum())
                if (difference >= 30 and histogram_change >= 0.25 and position - cuts[-1] >= 0.25
                        and expected_duration - position >= 0.25):
                    cuts.append(position)
            previous_image, previous_histogram = image, histogram
        if count == 0 or previous_pts is None or previous_pts - first_pts < expected_duration - 1:
            raise ValueError("Decoded stream did not cover the source duration")
    boundaries = [*cuts, expected_duration]
    return ([{"start": start, "end": end} for start, end in zip(boundaries, boundaries[1:])],
        {"decoded_frames": count, "container_duration_seconds": reported_duration, "pts_origin_seconds": first_pts,
         "runtime": "python3.12", "pyav_version": av.__version__, "numpy_version": np.__version__,
         "detector_profile": "abrupt_colour_changes", "thresholds": {"pixel_difference": 30, "histogram_l1": 0.25, "min_shot_seconds": 0.25}})
