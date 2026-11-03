"""Orion's screen vision: screenshots, OCR, and putting images on the clipboard.

Used by the "explain this", "read this", "summarize this page", "translate
this" and "take a screenshot" voice commands.
"""

import datetime
import io
import os

from PIL import ImageGrab

import config


def _screenshot_dir():
    """Return the Windows default Screenshots folder, creating it if needed."""
    path = os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots")
    os.makedirs(path, exist_ok=True)
    return path


def grab_screen():
    """Return a PIL Image of the whole screen, or None on failure."""
    try:
        return ImageGrab.grab()
    except Exception:
        return None


def take_screenshot():
    """Save a timestamped PNG of the whole screen to the Screenshots folder.

    Returns the saved file path, or None on failure.
    """
    image = grab_screen()
    if image is None:
        return None
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(_screenshot_dir(), f"orion_screenshot_{stamp}.png")
    try:
        image.save(path)
    except OSError:
        return None
    return path


def ocr_text(image=None):
    """Run OCR on `image` (or a fresh full-screen grab) and return the text."""
    try:
        import pytesseract
    except ImportError:
        return ""
    pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD
    if image is None:
        image = grab_screen()
    if image is None:
        return ""
    try:
        return pytesseract.image_to_string(image).strip()
    except Exception:
        return ""


def copy_image_to_clipboard(image):
    """Place a PIL image on the Windows clipboard as a DIB.

    Returns True on success. The AI bridge reads the image from the
    clipboard when sending a screenshot query.
    """
    try:
        import win32clipboard
    except ImportError:
        return False
    output = io.BytesIO()
    try:
        image.convert("RGB").save(output, "BMP")
        # A BMP file starts with a 14-byte header; the clipboard DIB format
        # (CF_DIB) expects the data without it.
        data = output.getvalue()[14:]
    finally:
        output.close()
    try:
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
        win32clipboard.CloseClipboard()
        return True
    except Exception:
        return False
