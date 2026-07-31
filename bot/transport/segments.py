"""Small constructors for validated OneBot message segments."""


def text_segment(text):
    return {"type": "text", "data": {"text": str(text)}}


def at_segment(user_id):
    return {"type": "at", "data": {"qq": str(user_id)}}


def reply_segment(message_id):
    return {"type": "reply", "data": {"id": str(message_id)}}


def image_segment(file):
    return {"type": "image", "data": {"file": str(file)}}
