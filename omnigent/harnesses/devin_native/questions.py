"""Translate Devin's native question inputs and web answers."""

from __future__ import annotations

from omnigent.util.json_types import JsonObject


def question_payload(tool_input: object) -> JsonObject | None:
    """Return the web question form, or defer malformed inputs to Devin."""
    if not isinstance(tool_input, dict) or isinstance(tool_input.get("answers"), dict):
        return None
    raw_questions = tool_input.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        return None
    questions: list[JsonObject] = []
    for raw in raw_questions:
        if not isinstance(raw, dict):
            return None
        text = raw.get("question")
        raw_options = raw.get("options")
        if not isinstance(text, str) or not text or not isinstance(raw_options, list):
            return None
        options: list[JsonObject] = []
        for raw_option in raw_options:
            if not isinstance(raw_option, dict):
                return None
            label = raw_option.get("label")
            if not isinstance(label, str) or not label:
                return None
            option: JsonObject = {"label": label}
            description = raw_option.get("description")
            if isinstance(description, str):
                option["description"] = description
            options.append(option)
        if not options:
            return None
        question: JsonObject = {
            "question": text,
            "options": options,
            "multiSelect": raw.get("multi_select") is True,
        }
        header = raw.get("header")
        if isinstance(header, str):
            question["header"] = header
        questions.append(question)
    return {"questions": questions}


def answered_question_input(tool_input: JsonObject, content: JsonObject) -> JsonObject:
    """Convert form values to Devin's selected/custom_text answer records.

    Missing answers stay absent so Devin treats them as skipped. Option labels
    are matched exactly, including labels containing commas.
    """
    payload = question_payload(tool_input)
    if payload is None:
        raise ValueError("Invalid Devin question input")
    questions = payload["questions"]
    assert isinstance(questions, list)
    answers: JsonObject = {}
    for question in questions:
        text = question["question"]
        if text not in content:
            continue
        value = content[text]
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
            raise ValueError("Question answers must be text or a list of text selections")
        if not question["multiSelect"] and len(values) > 1:
            raise ValueError("A single-choice question received multiple answers")
        labels = {option["label"] for option in question["options"]}
        selected = [value for value in values if value in labels]
        custom = [value for value in values if value not in labels and value]
        if not selected and not custom:
            continue
        answer: JsonObject = {"selected": selected}
        if custom:
            answer["custom_text"] = "\n".join(custom)
        answers[text] = answer
    return {**tool_input, "answers": answers}
