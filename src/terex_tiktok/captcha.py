from __future__ import annotations

import base64
import io
import re
import time
from collections import defaultdict

import requests
from PIL import Image

from .config import Config


class CaptchaProvider:
    """Configured Roboflow detector; bounded supported challenges, then operator."""
    def __init__(self, config: Config):
        self.config = config

    def try_challenge(self, page) -> bool:
        if not self.config.captcha_key or not self.config.captcha_model:
            return False
        deadline = time.monotonic() + 60
        image = page.locator("img#captcha-verify-image")
        if image.count() != 1 or not image.is_visible():
            return False
        for _ in range(2):
            if time.monotonic() >= deadline:
                break
            question = page.locator("div[class*='VerifyBar']").inner_text(timeout=3000).lower()
            model = self.config.captcha_model
            requested = None
            if "same" not in question:
                if not self.config.captcha_object_model:
                    return False
                model = self.config.captcha_object_model
                requested = question
            if not re.fullmatch(r"[A-Za-z0-9_-]+/\d+", model):
                return False
            content = image.screenshot(timeout=5000)
            if len(content) > 4 * 1024 * 1024:
                return False
            try:
                with requests.Session() as session:
                    session.trust_env = False
                    response = session.post(f"https://detect.roboflow.com/{model}",
                        params={"api_key": self.config.captcha_key}, data=base64.b64encode(content),
                        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=(5, 12))
                    response.raise_for_status()
                    predictions = response.json().get("predictions", [])
                groups = defaultdict(list)
                for prediction in predictions:
                    if float(prediction.get("confidence", 0)) >= 0.7:
                        groups[str(prediction["class"]).lower()].append(prediction)
                if requested:
                    selected = [box for label, boxes in groups.items() if label in requested for box in boxes]
                    if not 1 <= len(selected) <= 4:
                        return False
                else:
                    pairs = [boxes for boxes in groups.values() if len(boxes) == 2]
                    if len(pairs) != 1:
                        return False
                    selected = pairs[0]
                width, height = Image.open(io.BytesIO(content)).size
                bounds = image.bounding_box()
                if not bounds:
                    return False
                for box in selected:
                    if time.monotonic() >= deadline:
                        return False
                    x, y = float(box["x"]), float(box["y"])
                    if not 0 <= x <= width or not 0 <= y <= height:
                        return False
                    page.mouse.click(bounds["x"] + x * bounds["width"] / width, bounds["y"] + y * bounds["height"] / height)
                submit = page.locator("div.verify-captcha-submit-button")
                if submit.count() == 1:
                    submit.click(timeout=3000)
                page.wait_for_timeout(2000)
                if not image.is_visible():
                    return True
                refresh = page.locator("span.secsdk_captcha_refresh--text")
                if refresh.count() != 1:
                    break
                refresh.click(timeout=3000)
                page.wait_for_timeout(1000)
            except Exception:
                return False
        return False
