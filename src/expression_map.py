from collections.abc import Collection

# Each expression maps the model's tags to how strongly they point at it.
# 1.0 is the norm. Very common tags that show up on many kinds of face (blush
# and smile are each on ~2.4M training images) count less; tags that clearly
# name one expression count more. Every tag here exists in the model's
# selected_tags.csv: a tag the model can't output would never match.
EXPRESSION_TAGS: dict[str, dict[str, float]] = {
    "happy": {
        "smile": 1.0, "happy": 1.5, "grin": 1.0, ":d": 1.0, ";d": 1.0,
        ":3": 0.5, "^_^": 1.2, "light_smile": 0.7, "happy_tears": 1.5,
        "sparkling_eyes": 0.8, "excited": 1.2, "cheering": 1.0,
        "double_v": 0.5, "thumbs_up": 0.5,
    },
    # A laughing face also gets happy's smile, :d and ^_^, so the tags that
    # set it apart count extra to outweigh them.
    "laughing": {
        "laughing": 3.0, "xd": 2.0, "ojou-sama_pose": 1.5, ":d": 1.0,
    },
    "sad": {
        "sad": 1.5, "crying": 1.2, "crying_with_eyes_open": 1.2,
        "streaming_tears": 1.2, "tearing_up": 1.0, "wiping_tears": 1.0,
        "tears": 0.6, "teardrop": 1.0, "depressed": 1.5,
        "gloom_(expression)": 1.2, "frown": 0.5, "pout": 0.5, "u_u": 1.0,
    },
    "angry": {
        "angry": 1.5, "annoyed": 1.2, "anger_vein": 1.2, "glaring": 1.2,
        "scowl": 1.2, ">:(": 1.0, "furrowed_brow": 0.8, "clenched_teeth": 0.6,
        "shouting": 0.6, "puff_of_air": 0.6, "jitome": 0.6,
        "v-shaped_eyebrows": 0.4,
    },
    "surprised": {
        "surprised": 1.5, "wide-eyed": 1.0, "o_o": 1.0, "!": 0.8, "!!": 0.8,
        "!?": 0.8, ":o": 0.5,
    },
    "disgusted": {
        "disgust": 2.0, "grimace": 1.0,
    },
    "embarrassed": {
        "embarrassed": 2.0, "flustered": 2.0, "shy": 1.5,
        "full-face_blush": 1.5, "index_fingers_together": 1.5,
        "nervous_smile": 1.2, "nervous": 1.0, "nervous_sweating": 1.0,
        "wavy_mouth": 1.0, "averting_eyes": 1.0, "covering_face": 1.0,
        "spoken_blush": 1.0, "fidgeting": 1.0, "hands_on_own_cheeks": 0.8,
        "hands_on_own_face": 0.8, "ear_blush": 0.7, "flying_sweatdrops": 0.7,
        "covering_own_mouth": 0.7, "sweatdrop": 0.5, "nose_blush": 0.5,
        "playing_with_own_hair": 0.5, "blush": 0.4, "light_blush": 0.3,
    },
    "neutral": {
        "expressionless": 1.5, "blank_stare": 1.2, ":|": 1.0,
        "blank_eyes": 1.0, "empty_eyes": 1.0, "bored": 1.0, "serious": 0.7,
    },
    "fearful": {
        "scared": 2.0, "panicking": 1.2, "turn_pale": 1.0, "shaking": 1.0,
        "trembling": 0.8, "screaming": 0.8, "constricted_pupils": 0.6,
        "shaded_face": 0.5,
    },
    "smug": {
        "smug": 2.0, "doyagao": 2.0, "smirk": 1.5, "naughty_face": 1.2,
        "evil_smile": 1.2, "evil_grin": 1.2, "teasing": 1.2, ">:)": 1.0,
        ":>=": 1.0, "raised_eyebrow": 0.8, ":>": 0.6, "half-closed_eyes": 0.5,
    },
    "confused": {
        "confused": 2.0, "?": 1.0, "??": 1.0, "spoken_question_mark": 1.0,
        "@_@": 1.0, "head_tilt": 0.3,
    },
    # Pondering, often with a hand to the chin. A puzzled face with question
    # marks is confused instead.
    "thinking": {
        "thinking": 2.0, "stroking_own_chin": 1.5, "hand_on_own_chin": 1.2,
        "finger_to_own_chin": 1.2, "thought_bubble": 0.8, "finger_to_cheek": 0.5,
        "head_rest": 0.5,
    },
}

# "other" holds images with no recognisable expression, or where the face
# isn't the focus; it can also be picked by hand.
ALL_CATEGORIES = list(EXPRESSION_TAGS.keys()) + ["other"]

# The model is at least this sure of the tags it clearly sees in an image.
CLEAR_SCORE = 0.35


def score_expressions(tag_scores: dict[str, float]) -> dict[str, tuple[float, float]]:
    """How strongly each expression shows in an image, from all its tag scores.

    Each is a (clear, faint) pair of weighted totals. Clear counts only the
    tags the model clearly sees; faint counts every tag however unsure, to
    still tell which expression an image is closest to when it clearly shows
    none of those it may go to. Pairs compare by clear first, then faint.
    """
    scores = {}
    for expression, weights in EXPRESSION_TAGS.items():
        clear = faint = 0.0
        for tag, w in weights.items():
            score = tag_scores.get(tag, 0.0)
            faint += score * w
            if score >= CLEAR_SCORE:
                clear += score * w
        scores[expression] = (clear, faint)
    return scores


def classify_expression(
    scores: dict[str, tuple[float, float]], allowed: Collection[str] = ALL_CATEGORIES
) -> str:
    """Which of the allowed categories an image goes in, by score_expressions().

    An image goes to the allowed expression it shows most, so taking away its
    top one moves it to its next. With "other" allowed, it goes there instead
    if it clearly shows none of the allowed expressions. Without "other", it
    goes to the allowed expression it's closest to, however faintly.
    """
    picked = [e for e in EXPRESSION_TAGS if e in allowed]
    if not picked:
        return "other"

    best = max(picked, key=scores.__getitem__)
    clear = scores[best][0]
    return "other" if "other" in allowed and clear == 0 else best
