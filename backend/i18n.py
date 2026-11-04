"""What the worker hears and reads, in their language.

Only the words change. SAM phrases, the judge's success criteria and the audit
reasons stay as captured, so the checks behave the same in every language.
"""
from __future__ import annotations

LANG_NAMES = {"en": "English", "hi": "Hindi"}


def norm(lang: object) -> str:
    """A language this build speaks; anything else is English."""
    return lang if isinstance(lang, str) and lang in LANG_NAMES else "en"


# key -> (English, Hindi). The English side is what Ustad has always said.
PHRASES: dict[str, tuple[str, str]] = {
    "warning": ("Warning. {hazard} ", "चेतावनी। {hazard} "),
    "should_read": (" It should read {spec}.", " रीडिंग {spec} होनी चाहिए।"),
    "timer": (" Timer: {n} seconds.", " टाइमर: {n} सेकंड।"),
    "say_done_checked": (" Say done when you've checked it.", " जाँच लें, फिर बोलें: हो गया।"),
    "dark": ("I can't see anything. Check the camera lens is clear.",
             "मुझे कुछ दिख नहीं रहा। देख लें कि कैमरे का लेंस साफ़ है।"),
    "nothing_wrong": ("Nothing is wrong so far. Say done when the step is finished.",
                      "अभी तक सब ठीक है। काम पूरा होने पर बोलें: हो गया।"),
    "the_part": ("the part", "पुर्ज़ा"),
    "at_where": ("The {target} is at the {where} of your view. It's highlighted.",
                 "{target} आपके सामने {where} है। उसे हाइलाइट कर दिया है।"),
    "ref_hint": (", at the {where} of the expert's view", ", {where}"),
    "unseen_ref_where": ("I can't see the {target} in your view yet. Here it is in the expert's video{hint}. "
                         "Point the camera there.",
                         "मुझे {target} अभी दिख नहीं रहा। एक्सपर्ट के वीडियो में यह यहाँ है{hint}। कैमरा वहाँ घुमाएँ।"),
    "unseen_ref": ("I can't see the {target} yet. This is how the expert's view looked. Point the camera there.",
                   "मुझे {target} अभी दिख नहीं रहा। एक्सपर्ट का व्यू ऐसा था। कैमरा वहाँ घुमाएँ।"),
    "unseen": ("I can't see the {target} yet. Point the camera at the work.",
               "मुझे {target} अभी दिख नहीं रहा। कैमरा काम की तरफ़ घुमाएँ।"),
    "ask_wait": ("Give me a moment, then ask again.", "एक पल रुकें, फिर से पूछें।"),
    "ask_failed": ("Sorry, I couldn't work that out. Ask me again.", "माफ़ करें, समझ नहीं पाया। फिर से पूछें।"),
    "ask_unheard": ("Sorry, I didn't catch that. Ask me again.", "माफ़ करें, सुन नहीं पाया। फिर से पूछें।"),
    "expert_says": (" The expert says: {said}", " एक्सपर्ट कहते हैं: {said}"),
    "tip": (" Tip: {tip}", " सुझाव: {tip}"),
    "get_supervisor": ("Stop here and get your supervisor.", "यहीं रुकें और अपने सुपरवाइज़र को बुलाएँ।"),
    "hand_near": ("Hand near the {label}. Keep clear.", "हाथ {label} के पास है। दूर रहें।"),
    "unsafe": ("Stop. That is unsafe.", "रुकिए। यह सुरक्षित नहीं है।"),
    "seconds_left": ("{n} seconds left on the timer.", "टाइमर में {n} सेकंड बाकी हैं।"),
    "got_it": ("Got it.", "ठीक है।"),
    "done": ("Done.", "हो गया।"),
    "step_already": ("Step {a} is already done.", "स्टेप {a} पहले ही हो चुका है।"),
    "steps_already": ("Steps {a} to {b} are already done.", "स्टेप {a} से {b} पहले ही हो चुके हैं।"),
    "complete": ("Procedure complete. Well done.", "काम पूरा हुआ। शाबाश।"),
    "missing": ("Done, but step{s} {steps} couldn't be confirmed from the end result.",
                "काम हो गया, पर स्टेप {steps} की पुष्टि आख़िरी नतीजे से नहीं हो पाई।"),
    "skips_ok": ("Done. The skipped steps check out in the finished result.",
                 "काम हो गया। छोड़े गए स्टेप भी आख़िरी नतीजे में ठीक दिखते हैं।"),
    "skipped": ("Done, but step{s} {steps} were skipped and can't be confirmed.",
                "काम हो गया, पर स्टेप {steps} छोड़े गए थे, इसलिए उनकी पुष्टि नहीं हो सकती।"),
    "left_behind": ("Before you restart: the {left} is still in the work area. ",
                    "दोबारा चालू करने से पहले: {left} अभी भी काम की जगह पर है। "),
    "left_behind_banner": ("{left} still in the work area", "{left} अभी भी काम की जगह पर है"),
    "no_view": ("No camera view yet. Point the camera at the work.",
                "अभी कैमरे में कुछ नहीं दिख रहा। कैमरा काम की तरफ़ घुमाएँ।"),
    "better_view": ("I could not see that clearly. Hold the view steady on the part.",
                    "मुझे साफ़ नहीं दिखा। कैमरा पुर्ज़े पर स्थिर रखें।"),
    "reading_bad": ("Reading is {value} {unit}. It must be {spec}.", "रीडिंग {value} {unit} है। यह {spec} होनी चाहिए।"),
    "reading_fix": ("Adjust until the reading is in range, then say done.",
                    "रीडिंग सही सीमा में आने तक ठीक करें, फिर बोलें: हो गया।"),
    "no_translation": ("Could not prepare Hindi for this job. Continuing in English.",
                       "इस काम का हिन्दी अनुवाद नहीं हो पाया। अंग्रेज़ी में जारी रख रहे हैं।"),
}

# Where a part sits in the view, from Detection.describe().
_WHERE_HI = {
    "centre of frame": "बीच में",
    "top-left": "ऊपर बाईं ओर", "top-centre": "ऊपर बीच में", "top-right": "ऊपर दाईं ओर",
    "middle-left": "बीच में बाईं ओर", "middle-right": "बीच में दाईं ओर",
    "bottom-left": "नीचे बाईं ओर", "bottom-centre": "नीचे बीच में", "bottom-right": "नीचे दाईं ओर",
}


def t(lang: str, key: str, **kw: object) -> str:
    en, hi = PHRASES[key]
    return (hi if lang == "hi" else en).format(**kw)


def where(lang: str, text: str) -> str:
    return _WHERE_HI.get(text, text) if lang == "hi" else text


def reading_spec_hi(what: str, lo: float | None, hi: float | None, unit: str) -> str:
    if lo is not None and hi is not None:
        return f"{what} {lo:g} से {hi:g} {unit} के बीच".strip()
    if lo is not None:
        return f"{what} कम से कम {lo:g} {unit}".strip()
    return f"{what} ज़्यादा से ज़्यादा {hi:g} {unit}".strip()


def say_rule(lang: str, fields: str, keep_english: str) -> str:
    """The line added to a model's instructions for a non-English worker."""
    if lang != "hi":
        return ""
    return (
        "\nLANGUAGE: this worker speaks Hindi. Write " + fields + " in simple, everyday shop-floor Hindi, "
        "in Devanagari script, as one colleague talks to another. Use the Devanagari spelling of the English "
        "shop words workers really use (बोल्ट, वाल्व, स्पैनर, प्रेशर). Keep numbers as digits 0-9 and units exactly "
        "as given. The worker may speak Hindi or English. " + keep_english + "\n"
    )
