from __future__ import annotations

import re

DISPLAY_ALIASES = {
    "acidjazz": "Acid Jazz",
    "alternativerock": "Alternative Rock",
    "bluesrock": "Blues Rock",
    "bossanova": "Bossa Nova",
    "classicrock": "Classic Rock",
    "darkambient": "Dark Ambient",
    "deephouse": "Deep House",
    "drumnbass": "Drum n Bass",
    "easylistening": "Easy Listening",
    "electropop": "Electropop",
    "hardrock": "Hard Rock",
    "hiphop": "Hip Hop",
    "instrumentalpop": "Instrumental Pop",
    "instrumentalrock": "Instrumental Rock",
    "jazzfusion": "Jazz Fusion",
    "newage": "New Age",
    "newwave": "New Wave",
    "pipeorgan": "Pipe Organ",
    "popfolk": "Pop Folk",
    "poprock": "Pop Rock",
    "postrock": "Post Rock",
    "punkrock": "Punk Rock",
    "rnb": "R&B",
    "rocknroll": "Rock & Roll",
    "singersongwriter": "Singer-Songwriter",
    "synthpop": "Synth-pop",
    "triphop": "Trip Hop",
    "worldfusion": "World Fusion",
    "acousticbassguitar": "Acoustic Bass Guitar",
    "acousticguitar": "Acoustic Guitar",
    "classicalguitar": "Classical Guitar",
    "doublebass": "Double Bass",
    "drummachine": "Drum Machine",
    "electricguitar": "Electric Guitar",
    "electricpiano": "Electric Piano",
}


def readable_label(label: str) -> str:
    cleaned = label.strip()
    if not cleaned:
        raise ValueError("labels cannot be empty")
    if cleaned in DISPLAY_ALIASES:
        return DISPLAY_ALIASES[cleaned]
    value = cleaned.replace("_", " ").replace("-", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value.title()
