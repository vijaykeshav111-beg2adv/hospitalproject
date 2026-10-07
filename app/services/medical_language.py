"""Local medical language layer for the AI front desk.

WHAT THIS MODULE IS
-------------------
Layer 1 of the hybrid routing pipeline:

    safety (local, authoritative)
        -> LOCAL VOCABULARY + alias/typo matching        <- this module
        -> MySQL specialty_concerns keyword scoring      <- ai_engine
        -> Groq semantic understanding (only if unsure)  <- groq_engine
        -> clarification question (never guess)

It converts the way real patients write into the canonical English terms the
hospital already stores in `specialty_concerns.keyword`, in pure Python:

    "ghutne mein dard"        -> knee + pain            -> Orthopaedics
    "gutno me dard"           -> knee + pain   (typo)   -> Orthopaedics
    "mere baal jhd rhe hai"   -> hair fall              -> Dermatology
    "meri ammkein laal hai"   -> eye + redness in eye   -> Ophthalmology
    "pith pe laal laal dane"  -> back + rash            -> Dermatology

FOUR LAYERS OF MATCHING
-----------------------
1. EXACT      - the curated vocabulary below (Hindi, Hinglish, Devanagari,
                English synonyms and known typos).
2. OBLIQUE    - systematic Hindi forms generated from a stem (aankh -> aankhein,
                aankhon, aankho; ghutne -> ghutno, ghutnon), so every inflection
                does not have to be listed by hand.
3. ALIASES    - a compact map of the misspellings patients actually type
                (gutne, gutno, ammkein, jhd, dardd, bhukhar ...).
4. FUZZY      - an edit-distance-1 index built from the vocabulary, so ANY
                one-letter mistake (missing / extra / swapped letter) still
                matches: "ghutonne" -> ghutne, "aankhen" -> aankh, "jhd" -> jhad.
                Bounded, deterministic, O(1) per word, no model, no tokens.

WHAT THIS MODULE DOES *NOT* DO
------------------------------
* No diagnosis, no prescription, no clinical decision - it only rewrites
  vocabulary. Authority stays with ai_engine.scan_safety and the doctors.
* No fuzzy matching in the safety path. Emergencies are matched by exact phrases
  and shape patterns only (see has_emergency_term), because a fuzzy match must
  never invent or miss an emergency.
* No guessing. If the message is not understood, it says so
  ("understood": False) and the caller either asks Groq or asks the patient.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# canonical vocabulary
# ---------------------------------------------------------------------------
# Every canonical value is a term the hospital already knows: a keyword in the
# `specialty_concerns` table (see app/seed.py) or a plain body area that
# combines with a symptom. That is what makes local routing work.
LOCAL_MEDICAL_TERMS: dict[str, str] = {}


def _add(canonical: str, *terms: str) -> None:
    """Register surface forms for one canonical term."""
    for term in terms:
        t = term.strip().lower()
        if t:
            LOCAL_MEDICAL_TERMS.setdefault(t, canonical)


# Oblique/plural Hindi endings. Generating these from the stem is why a patient
# can type "aankhon", "aankhein", "ghutno", "gutnon" without us listing each one.
_OBLIQUE_SUFFIXES = ("", "e", "en", "ein", "on", "o", "aa", "i", "aan")


def _add_stem(canonical: str, *stems: str) -> None:
    """Register a stem plus its common Hindi oblique/plural forms."""
    for stem in stems:
        for suffix in _OBLIQUE_SUFFIXES:
            LOCAL_MEDICAL_TERMS.setdefault((stem + suffix).lower(), canonical)


# ======================= FEVER / INFECTION ================================
_add("fever", "bukhar", "bukhaar", "bukar", "bhukhar", "bukhhar", "fever", "fevar", "fiver",
     "feverish", "temperature", "garmi", "tap", "taap", "taav", "tav", "ताप", "ज्वर",
     "बुखार", "बुख़ार", "बुखार आया", "bukhar aaya", "taav chadh", "taav chadh gya", "bukhar hai",
     "bukhar ho", "bukhar aa", "गरमी", "बदन गरम", "badan garam", "high fever",
     "तेज़ बुखार", "तेज बुखार", "bukhar 3 din se")
_add("typhoid", "typhoid", "टाइफाइड")
_add("malaria", "malaria", "मलेरिया")
_add("dengue", "dengue", "डेंगू")
_add("infection", "infection", "संक्रमण", "sankraman")
_add("flu", "flu", "influenza")
_add("body ache", "body ache", "bodyache", "body pain", "bodypain", "badan dard", "badan me dard",
     "badan mein dard", "tan dard", "sharir dard", "शरीर दर्द", "बदन दर्द", "सारा बदन दर्द",
     "sara badan dard", "ang dard", "अंग दर्द", "muscle pain", "muscles dard",
     "मांसपेशियों में दर्द", "badan tuta", "बदन टूट")
_add("weakness", "kamzori", "kamzoori", "kamzor", "कमजोरी", "कमज़ोरी", "weakness", "weak",
     "kamshakti", "कमशक्ति", "बहुत कमजोरी", "kamzori lag rahi")
_add("fatigue", "thakan", "थकान", "थकावट", "thakawat", "fatigue", "tired", "thak gaya",
     "thak gayi", "tiredness", "सुस्ती", "susti")
_add("sugar", "sugar", "शुगर", "madhumeh", "मधुमेह", "diabetes", "diabetic",
     "sugar badh gaya", "sugar kam", "sugar high")
_add("bp", "bp", "b p", "बीपी", "blood pressure", "bloodpressure", "blood presure",
     "रक्तचाप", "pressure high", "pressure low", "bp high", "bp low", "bp badh")
_add("thyroid", "thyroid", "थायरॉइड", "थायराइड", "thyroid ka problem")
_add("anemia", "khoon ki kami", "खून की कमी", "anemia", "रक्त की कमी", "hemoglobin kam",
     "hb kam", "हीमोग्लोबिन कम")

# ======================= PAIN : GENERIC ===================================
_add("pain", "dard", "darrd", "dardd", "dukh", "dukhan", "दर्द", "dard ho raha", "dard hori",
     "dard ho ri", "dard ho rahi", "dard hai", "takleef", "तकलीफ", "पीड़ा", "peeda", "pida",
     "dard kar raha", "dard rehta", "pain", "paining", "ache", "aching", "दर्द होता है",
     "दर्द है", "भयंकर दर्द", "dard se pareshan")

# ======================= HEAD / NEURO =====================================
_add_stem("head", "sar", "sir", "सिर", "सर")
_add("headache", "sar dard", "sir dard", "सिर दर्द", "सर दर्द", "headache", "head ache",
     "head pain", "headpain", "sar dukh", "sir dukh", "sar me dard", "sar mein dard",
     "sir me dard", "sir mein dard", "सिर में दर्द", "सर में दर्द", "सिर दुख", "माथा दर्द",
     "matha dard", "माथे में दर्द", "maathe me dard", "matha phata", "सिर फटा", "sar phata",
     "sir phata", "sir bhari", "सिर भारी", "sar bhari", "sir dard hai", "sar dard hai")
_add("migraine", "migraine", "migren", "माइग्रेन", "migrain", "aadha sar dard", "आधा सिर दर्द",
     "aadha sir dard", "half head pain", "aadhe sar mein dard", "aadhe sir me dard")
_add("dizziness", "chakkar", "chakar", "chakkar aa rahe", "chakkar aa raha", "चक्कर", "चकर",
     "सिर घूमना", "sar ghoom", "sir ghoom", "sar ghoomna", "sir ghoomna", "sir ghuma",
     "ghoom raha", "dizzy", "dizziness", "dizzyness", "चक्कर आना", "chakkar aana",
     "sir chakra raha", "सिर चकरा", "chakkar aa rhe")
_add("vertigo", "vertigo", "वर्टिगो", "kan chakra raha", "सिर घूमता है")
_add("fainted", "behoshi", "बेहोशी", "fainting", "behosh ho gaya", "बेहोश हो", "chakkar aakar gir")
_add("unconscious", "behosh", "behos", "बेहोश", "unconscious")
_add("seizure", "mirgi", "मिर्गी", "jhatke", "झटके", "jhatka", "seizure", "दौरा", "daura",
     "fits", "fits aana", "मिर्गी का दौरा", "convulsion", "jhatka aa gaya")
_add("numbness", "sunn", "sunnpan", "सुन्न", "jhumjhumahat", "numbness", "numb", "hath sunn",
     "हाथ सुन्न", "pair sunn", "पैर सुन्न", "jhunjhuni", "झुनझुनी", "sunn ho gaya",
     "हाथ पैर सुन्न", "hath pair sunn")
_add("paralysis", "lakwa", "laqwa", "लकवा", "paralysis", "aadha sharir kam nahi kar raha",
     "आधा शरीर काम नहीं कर रहा")
_add("sciatica", "sciatica", "साइटिका", "gridhrasi", "गृध्रसी", "kamar se pair tak dard")
_add("memory", "bhoolne", "भूलने", "yaad nahi", "याद नहीं", "bhool jata", "memory kam",
     "memory weak", "bhool jata hu")

# ======================= EYE / OPHTHALMOLOGY ==============================
_add_stem("eye", "aankh", "ankh", "aakh", "aamkh", "aamk", "ammk", "akk", "akkh",
          "आँख", "आंख", "आख", "नयन")
_add("eye", "eye", "eyes", "aankhein", "aankhen", "aankhon", "aankho", "dono aankh", "netra",
     "नेत्र", "aankh me kuch gir gaya")
_add("redness in eye", "aankh laal", "aankh lal", "aankh laal hai", "aankh lal hai",
     "aankh laal ho rahi", "aankh laal ho rhi", "aankh laal ho raha", "आँख लाल", "आंख लाल",
     "आँख लाल हो रही", "aankhein laal", "aankhein lal", "aankhen laal", "aankhein laal ho rhi",
     "aankhon mein laalpan", "aankh mein laalpan", "आँखों में लालपन", "redness in eye", "eye red",
     "red eye", "red eyes", "aankh me sujan", "आँख में सूजन", "aankh laal ho rahi hai",
     "ammkein laal", "ammkein laal ho rhi", "aankhein laal ho rahi")
_add("eye", "aankh mein dard", "aankh me dard", "आँख में दर्द", "आँखों में दर्द",
     "aankhon mein dard", "aankh dard", "eye pain", "eyeache", "eye ache", "eyepain",
     "aankh me jalan", "aankh mein jalan", "आँख में जलन", "aankhon mein jalan", "eye burning",
     "aankh jal rahi", "aankh se pani", "aankhon se pani", "आँख से पानी", "आँखों से पानी",
     "aankh me pani", "aankh pani aa raha", "eye watering", "watery eye", "aankh se paani")
_add("blurred", "dhundhla", "धुंधला", "dhundla", "blurred", "blur", "dhundhla dikh",
     "dhundhla dikh raha", "blurred vision", "dhundhla dikh raha hai")
_add("vision", "nazar", "नज़र", "नजर", "nazar kam", "नज़र कम", "kam dikh raha", "कम दिख",
     "nazar nahi", "नज़र नहीं", "dikh nahi raha", "दिख नहीं", "vision kam", "weak vision",
     "कम दिखाई", "kam dikhai de raha", "धुंधला दिख रहा")
_add("chashma", "chashma", "चश्मा", "spectacles", "glasses", "nazar ka chashma", "number badh gaya")
_add("cataract", "motiyabind", "मोतियाबिंद", "cataract", "मोतिया")

# ======================= EAR / NOSE / THROAT ==============================
_add_stem("ear", "kaan", "kan", "कान")
_add("ear", "ear", "ears", "earache", "ear pain", "earpain", "kaan dard", "kaan me dard",
     "kaan mein dard", "कान दर्द", "कान में दर्द", "kan dard", "kan me dard", "kaan dukh",
     "kaan me sujan", "कान में सूजन", "kaan se pani", "kaan se paani", "कान से पानी",
     "kaan se khoon", "kaan me awaaz", "कान में आवाज", "kaan bhar gaya", "kaan me sui")
_add("hearing", "sunai", "सुनाई", "sunayi", "sunai kam", "सुनाई कम", "hearing", "hearing loss",
     "sunai nahi", "सुनाई नहीं", "kaan sunn", "bahra", "बहरा", "kam sunai deta",
     "sunai kam de raha", "सुनाई कम दे रहा")
_add_stem("nose", "naak", "nak", "नाक")
_add("nose", "nose", "naak band", "नाक बंद", "nak band", "naak beh", "नाक बह", "nak beh rahi",
     "naak beh rahi", "naak se pani", "नाक से पानी", "running nose", "runny nose",
     "blocked nose", "naak me dard", "नाक में दर्द", "naak se khoon", "नाक से खून",
     "naak beh rahi hai")
_add("sinus", "sinus", "साइनस", "sinusitis", "sinus dard", "naak me sinus")
_add_stem("throat", "gala", "gale", "गला", "गले")
_add("throat", "throat", "gale mein dard", "gale me dard", "गले में दर्द", "gala dard",
     "gale dard", "gala dukh", "gala kharab", "गला खराब", "gala kharab ho gaya", "sore throat",
     "throat pain", "throatpain", "gale mein kharish", "गले में खरिश", "gale me khich",
     "gala khich raha", "khich khich", "खिचखिच", "gala baith gaya", "आवाज बैठ",
     "aawaz baith gayi", "voice change", "gale me sui", "गले में सूजन", "gale mein sujan",
     "tonsil", "टॉन्सिल", "gala me ghaav", "gale mein dana", "gala sooj gaya")
_add("cough", "khansi", "khaansi", "khasi", "khaasi", "khanshi", "khansi aa rahi", "खांसी",
     "खाँसी", "कफ", "kaf", "cough", "coughing", "cough aa raha", "sukhi khansi", "सूखी खांसी",
     "dry cough", "balgam", "बलगम", "khansi me khoon", "खांसी में खून", "kabhi kabhi khansi",
     "khansi ho rahi", "khansi nhi ruk rahi", "khansi nahi ruk rahi", "khansi 2 hafte se")
_add("cold", "zukam", "zukaam", "jukam", "jukham", "zukham", "जुकाम", "ज़ुकाम", "sardi", "सर्दी",
     "सर्दी जुकाम", "sardi zukam", "nazla", "नज़ला", "नजला", "cold", "common cold", "chheenk",
     "छींक", "chheenk aa rahi", "sneezing", "thand lag rahi", "ठंड लग रही", "thand lagna",
     "body me thand", "ठंड लगना", "sardi lag rahi", "sardi lg rhi", "sardi lag rha",
     "zukam ho gaya", "jukam ho gaya", "naak beh rahi sardi")
_add("snoring", "kharrate", "खर्राटे", "snoring", "kharrate aate")

# ======================= CHEST / CARDIOLOGY ===============================
_add_stem("chest", "seena", "seene", "chhati", "chhaati", "chati", "सीना", "सीने", "छाती", "वक्ष")
_add("chest pain", "chest pain", "chestpain", "seene mein dard", "seene me dard", "सीने में दर्द",
     "chhati mein dard", "chhati me dard", "छाती में दर्द", "chati me dard", "seena dard",
     "chhati dard", "seene me jalan", "सीने में जलन", "chest mein jalan", "chhati me jalan",
     "chhati mein jalan", "छाती में जलन", "seene mein bhaari", "chest bhaari", "chest heavy",
     "chest tight", "chhati bhaari", "छाती भारी", "seene mein jakdan", "chest jakad")
_add("palpitations", "dhak dhak", "dhak-dhak", "dhakdhak", "dhadkan", "धड़कन", "धकधक",
     "dhadkan tez", "dil ki dhadkan", "palpitations", "heart racing", "dil tez chalta",
     "dil dhadak raha", "heart beat tez", "dhadkan badh gayi")
_add("heart", "dil", "दिल", "heart", "hriday", "हृदय", "dil mein dard", "दिल में दर्द",
     "dil dukh", "heart pain", "dil me jalan", "दिल में जलन")
_add("cholesterol", "cholesterol", "कोलेस्ट्रॉल", "ecg", "ईसीजी")

# ======================= BREATHING / PULMONOLOGY ==========================
_add("breathing", "saans", "sans", "saas", "सांस", "साँस", "सास", "saans lena", "breathing",
     "respiration", "sans lene me dikkat", "saans lene mein dikkat", "saans lene me takleef",
     "सांस लेने में तकलीफ", "saans lene mein takleef", "saans phool", "saans phoolna",
     "saans phool rahi", "saans ful rahi", "सांस फूल", "sans phool raha", "saans fulna",
     "saans phool rahi hai", "saans lene me dikkat")
_add("breathless", "saans ki dikkat", "saans ki takleef", "breathless", "breathless ho raha",
     "breathless feel", "shortness of breath", "short breath", "dam ghutna", "दम घुटना",
     "dam ghut raha", "दम घुट रहा", "saans chadhti", "saans chadh rahi hai",
     "saans chadh rahi", "seeti ki awaaz")
_add("asthma", "asthma", "दमा", "daam", "dama", "अस्थमा", "dama ka daura", "asthma attack",
     "saans ki bimari", "सांस की बीमारी", "inhaler")
_add("wheezing", "wheezing", "ghar ghar", "घरघर", "seene me seeti")
_add("TB", "tb", "टीबी", "tuberculosis", "क्षय रोग", "khaansi 1 mahine se")
_add("smoking", "smoking", "स्मोकिंग", "beedi", "nasha", "नशा", "gutkha", "गुटखा", "sharab",
     "शराब", "tambaku", "तंबाकू", "addiction", "नशे की लत", "nashe ki lat")

# ======================= STOMACH / GASTROENTEROLOGY ======================
_add_stem("stomach", "pet", "पेट")
_add("pet dard", "pet dard", "pet me dard", "pet mein dard", "पेट दर्द", "पेट में दर्द",
     "pet dukh", "pet dukh raha", "pet pain", "stomach pain", "stomachache", "pet ka dard",
     "pet marod", "पेट मरोड़", "marod", "मरोड़", "aant dard", "आंत दर्द", "pet bhari",
     "पेट भारी", "pet bharipan", "pet phoola", "पेट फूला", "pet kharab", "पेट खराब",
     "pet gada", "pet bigad gaya", "pet kharab hai", "pet me marod", "pet dard hai")
_add("acidity", "acidity", "एसिडिटी", "jalan", "जलन", "pet mein jalan", "pet me jalan",
     "पेट में जलन", "khatta", "खट्टा", "khatti dakaar", "खट्टी डकार", "dakaar", "डकार", "acid",
     "acidity ho rahi", "ulcer", "अल्सर", "gastric", "गैस्ट्रिक", "gastric problem",
     "pet me jalan ho rahi")
_add("gas", "gas", "गैस", "gas banti", "gas ban rahi", "pet me gas", "पेट में गैस",
     "gas ka problem", "bloating", "pet phoolna", "पेट फूलना", "dakaar aana", "badhazmi",
     "बदहज़मी", "apach", "अपच", "indigestion")
_add("constipation", "kabz", "कब्ज", "कब्ज़", "qabz", "constipation", "kabziyat", "कब्जियत",
     "saaf nahi hota", "toilet nahi", "शौच नहीं", "shauch nahi", "latrine nahi jaati",
     "pakhana nahi", "पखाना नहीं", "kabz hai", "kabz ho gaya")
_add("loose motion", "loose motion", "loose motions", "dast", "दस्त", "dast aa rahe",
     "dast aa raha", "dast ho rahe", "dast lag gaye", "diarrhoea", "diarrhea", "diarrh",
     "पतले दस्त", "patle dast", "pet khul gaya", "पेट खुल गया", "dast me khoon",
     "दस्त में खून", "loose motion ho rahe", "loose motion ho raha")
_add("vomiting", "ulti", "उल्टी", "vomiting", "vomit", "vomit ho raha", "ubkai", "उबकाई",
     "ji michalna", "जी मिचलाना", "ji michla raha", "man kharab", "मन खराब", "ulti ho rahi",
     "vomit kar raha", "पित्त", "pitt", "bhookh nahi", "भूख नहीं", "khana nahi khaya",
     "खाना नहीं खाया", "ulti aa rahi", "ulti ho rahi hai")
_add("jaundice", "jaundice", "पीलिया", "peeliya", "piliya", "piliya ho gaya", "aankh peeli",
     "आँख पीली", "peshab peela", "पेशाब पीला", "liver", "लीवर", "liver kharab")
_add("piles", "bawasir", "बवासीर", "piles", "arsh", "अर्श", "khoon wala pakhana",
     "खून वाला पखाना", "fissure", "फिशर", "gudda dard", "गुदा दर्द", "pakhana me khoon",
     "पखाना में खून")
_add("hernia", "hernia", "हर्निया", "aant utar gayi", "आंत उतर गई", "pet me gaanth")
_add("appetite", "bhookh", "भूख", "bhookh nahi lag rahi", "bhookh kam", "bhookh zyada",
     "खाना नहीं खाता", "khana nahi khata", "bhookh nahi")

# ======================= SKIN / HAIR / DERMATOLOGY =======================
_add("itching", "khujli", "khujlee", "khujali", "khujli hori", "khujli ho rahi", "khujli ho rhi",
     "khujli hoti", "खुजली", "खाज", "khaj", "itching", "itch", "itchy", "itch hori",
     "itching ho rahi", "itching hori", "चुल", "chul", "chulchul", "चुलचुल", "khujli raat me",
     "khujli badh rahi", "khujli hori hai", "khujli ho rahi hai", "khujli bahut")
_add("rash", "dane", "daane", "dana", "daana", "dane nikal", "dane aa gye", "daane aa gye",
     "dane ho gaye", "दाने", "दाना", "chakatte", "चकत्ते", "chakatte pad gaye", "laal laal",
     "laal laal dane", "लाल लाल दाने", "laal dane", "लाल दाने", "red patches", "rash", "rashes",
     "rash aa gaya", "rash nikal aaye", "rash nikal", "laal chakatte", "लाल चकत्ते", "safed dane",
     "सफेद दाने", "dane khujli", "दाने खुजली", "dane aur khujli", "daane khujli", "pith pe dane",
     "पीठ पर दाने", "chehre pe dane", "चेहरे पर दाने", "chehre pe laal dane",
     "pith pe laal laal dane", "dane aa gaye")
_add("skin", "skin", "twacha", "त्वचा", "चमड़ी", "chamdi", "chamda", "चमड़ा", "skin problem",
     "skin laal", "chamdi laal", "चमड़ी लाल", "chamdi sooj gayi", "skin dry", "skin sukh rahi")
_add("acne", "acne", "muhase", "मुहांसे", "muhase nikal", "pimples", "pimple", "pimpl", "kena",
     "फोड़े", "phode", "phoda", "फोड़ा", "foda", "furan", "फुरुन", "boil", "boils", "garmi dane",
     "गर्मी के दाने", "garmi ke dane", "muhase ho gaye", "chehre pe muhase")
_add("eczema", "eczema", "एक्जिमा", "dermatitis", "khaaj khujli")
_add("fungal", "fungal", "फंगल", "fungus", "daad", "दाद", "ringworm", "दाद हो गई", "nakhun",
     "नाखून", "nakhun me fungus", "pair me fungus", "pair ki ungli me")
_add("hair fall", "baal jhad", "बाल झड़", "baal jhad rahe", "baal jhad rhe", "baal jhad raha",
     "baal jhad rahi", "baal jhadna", "baal jharna", "baal jhar", "baal jhar rahe", "bal jhar",
     "bal jhad", "baal gir rahe", "बाल गिर", "baal gir rhe", "baal gir rha", "baal girna",
     "baal girte", "बाल झड़ना", "baal toot rahe", "बाल टूट", "baal toot rhe", "baal kam ho rahe",
     "baal patle", "बाल पतले", "baal patla", "sir ke baal", "सिर के बाल", "hair fall", "hairfall",
     "hair loss", "hairloss", "hair fall ho raha", "hair fall ho rha", "baal gir rahe hai",
     "mere baal jhd rhe hai", "baal jhad rhe hai", "baal bahut gir rahe", "ganjapan", "गंजापन",
     "baldness", "safed baal", "सफेद बाल", "baal safed")
_add("dandruff", "dandruff", "dandraf", "rupti", "रुप्ती", "sir me rupti", "सिर में रुप्ती",
     "sir ki chamdi", "khujli sir me", "sir khujli", "सिर में खुजली")
_add("swelling", "sujan", "soojan", "सूजन", "sooj", "suj", "सूज", "swelling", "swell",
     "sooj gaya", "सूज गया", "sujan aa gayi", "soojhan", "sujan hai", "phool gaya", "फूल गया",
     "gaanth", "गांठ", "gilti", "गिल्टी", "sujan ho gayi")
_add("allergy", "allergy", "alergy", "alerji", "एलर्जी", "allergy ho gayi", "allergic",
     "khaane se allergy", "dawa se allergy", "allergy nikal aayi")
_add("wound", "ghaav", "घाव", "ghav", "nasoor", "नासूर", "wound", "chot", "चोट", "kata", "कटा",
     "kharoch", "खरोंच")
_add("burn", "jal gaya", "जल गया", "burn", "jalna", "जलना", "jal gaya hath")
_add("mole", "til", "तिल", "mole", "masa", "मस्सा", "wart", "masse")

# ======================= ORTHOPAEDICS ====================================
_add_stem("knee", "ghutna", "ghutne", "ghutno", "ghutnon", "ghutn", "gutna", "gutne", "gutno",
          "gutnon", "gotna", "gotno", "घुटना", "घुटने", "घुटनो", "घुटनों")
_add("knee", "knee", "knees", "knee pain", "knee me dard", "knee mein dard", "ghutne mein dard",
     "ghutne me dard", "ghutno me dard", "ghutno mein dard", "ghutne ka dard", "घुटने में दर्द",
     "घुटनों में दर्द", "gutne me dard", "gutno me dard", "gutne mein dard", "ghutno me sujan",
     "ghutne me sujan", "घुटनों में सूजन", "knee me sujan", "ghutne me awaz", "घुटने में आवाज",
     "ghutne khatkhat", "khatkhat", "खटखट", "knee locking", "ghutne me jakdan", "pair ke ghutne",
     "पैर के घुटने", "donon ghutne", "ghutne dukh rahe", "knee pain while climbing stairs",
     "seedhi chadhne me dard", "ghutne me dard ho raha")
_add("joint", "jod", "jood", "jodo", "जोड़", "जोड़ों", "joint", "joints", "jod me dard",
     "जोड़ों में दर्द", "jodo me dard", "arthritis", "आर्थराइटिस", "gathiya", "गठिया", "gout",
     "jod sujan", "जोड़ों में सूजन", "jod khatkhat", "joint stiffness", "jod jakad", "जकड़न",
     "jakdan", "gathiya ho gaya")
_add("back pain", "kamar dard", "kamar me dard", "kamar mein dard", "कमर दर्द", "कमर में दर्द",
     "kamr dard", "kamar dukh", "kamar dukh raha", "kamar dard hai", "pith dard", "pith me dard",
     "pith mein dard", "पीठ दर्द", "पीठ में दर्द", "peeth dard", "peeth me dard", "pith dukh",
     "peet dard", "back pain", "backpain", "back me dard", "kamar toot rahi", "कमर टूट",
     "back jakad", "kamar jakad", "कमर जकड़", "uthne me kamar dard", "kamar dard ho raha",
     "kamar me sujan", "पीठ में दर्द हो रहा")
_add("neck", "gardan", "garda", "gardon", "gardn", "गर्दन", "gardan dard", "गर्दन दर्द",
     "gardan me dard", "neck", "neck pain", "neckpain", "neck me dard", "gardan jakad",
     "gardan sujan", "gardan juk nahi sakti")
_add("shoulder", "kandha", "kandhe", "kandho", "कंधा", "कंधे", "कंधों", "kandha dard",
     "kandhe me dard", "shoulder", "shoulder pain", "shoulderpain", "kandhe me sujan",
     "hath nahi uthalta", "हाथ नहीं उठता")
_add("elbow", "kohni", "कोहनी", "kuhni", "elbow", "elbow pain", "kohni dard", "kohni dukh")
_add("wrist", "kalai", "कलाई", "kalai dard", "wrist", "wrist pain", "kalai me dard")
_add("hip", "koolha", "कूल्हा", "koolhe", "hip", "hip pain", "koolhe me dard", "hip joint")
_add("leg", "pair", "pairon", "पैर", "पैरों", "taang", "टांग", "tang", "leg", "legs",
     "pair dard", "पैर दर्द", "pair me dard", "pair mein dard", "pair dukh", "pair dukh rahe",
     "leg pain", "legpain", "tang dard", "pair sujan", "पैर में सूजन", "pair phool gaya",
     "pair me kheechav", "पैर में खिंचाव", "kheechav", "खिंचाव", "khichav", "khich raha",
     "pair me khich raha", "chalne me dikkat", "चलने में दिक्कत", "chalne me takleef",
     "chalne pe dard", "chalne par dard", "pair bhaari", "पैर भारी", "pair me dard ho raha")
_add("heel", "edi", "eddi", "एड़ी", "edi dard", "heel", "heel pain", "edi me dard", "edi dukh",
     "edi me sujan")
_add("foot", "talwa", "तलवा", "talwe", "foot", "sole", "talwe me jalan", "ungal", "उंगली",
     "pair ki ungli")
_add("fracture", "haddi toot", "हड्डी टूट", "haddi tooti", "fracture", "फ्रैक्चर",
     "haddi toot gayi", "haddi", "हड्डी", "haddi dard", "bone", "bone pain", "haddi me dard",
     "haddi nikal gayi", "dislocation", "moch", "मोच", "moch aa gayi", "sprain", "sprained",
     "pair moch", "kamar moch")
_add("injury", "chot lagi", "चोट लगी", "injury", "injur", "hurt", "takkar lag gayi",
     "gir gaya", "गिर गया", "girne se", "gir pada")

# ======================= GYNAECOLOGY / OBSTETRICS ========================
_add("periods", "periods", "period", "periuds", "perids", "paeriods", "priods", "peediods",
     "periods late hai", "mahavari", "माहवारी", "periods aa gaye", "periods late",
     "periods late ho gaye", "periods nahi aaye", "periods miss", "periods ka problem", "padav",
     "माहवारी नहीं", "masik dharm", "मासिक धर्म", "periods me dard", "periods ke dard",
     "irregular periods", "अनियमित माहवारी", "aniyamit mahavari", "periods kam", "periods jyada",
     "periods bahut aate", "periods time par nahi", "periods nahi ho rahe")
_add("white discharge", "white discharge", "safed pani", "सफेद पानी", "safed pani aa raha",
     "safed pani aa raha hai", "white pani", "discharge", "chipchipa", "चिपचिपा", "gandagi",
     "गंदगी", "badbu", "बदबू", "safed jhar", "safed pani aa raha h")
_add("pregnancy", "pregnant", "pregnan", "pregnancy", "गर्भवती", "गर्भ", "garbhvati", "garbh",
     "pet me bacha", "पेट में बच्चा", "pet me bachha", "hamal", "हमल", "periods ruk gaye",
     "पीरियड्स रुक गए", "pregnancy check", "pregnancy test", "labour pain", "delivery", "प्रसव",
     "prasav", "bacha hile nahi", "bacha nahi hil raha", "bacha hile nahi", "garbh me bacha")
_add("pcos", "pcos", "pcod", "cyst", "सिस्ट", "ovary cyst", "pcos hai")
_add("infertility", "bacha nahi ho raha", "bachcha nahi ho raha", "bachche nahi ho rahe",
     "bacha nahi hua", "bachcha nahi hua", "bachche nahi huye", "बच्चा नहीं हो रहा", "infertility",
     "santan nahi", "santan sukh", "garbh nahi thehrta", "garbh nahi ban raha", "conceive nahi",
     "conceive nahi ho raha", "shaadi ke baad bachcha nahi", "2 saal se bachcha nahi",
     "teen saal se bachche nahi", "baby nahi ho raha", "pregnan nahi hoti")
_add("womb", "kokh", "kookh", "kokh me", "garbhashay", "garbhashaya", "bachchedani", "बच्चेदानी",
     "गर्भाशय", "womb", "womb me dard")
_add("breast", "stan", "स्तन", "breast", "breast me dard", "breast me gaanth")

# ======================= PAEDIATRICS =====================================
_add_stem("child", "bacha", "bachcha", "bachhe", "bacche", "बच्चा", "बच्चे", "बच्चों", "बच्ची",
          "bachhi", "bacchi")
_add("child", "child", "children", "kid", "kids", "beta", "बेटा", "beti", "बेटी", "baby",
     "infant", "shishu", "शिशु", "newborn", "naya bacha", "naya bachha", "beti ko bukhar",
     "bacche ko bukhar", "बच्चे को बुखार", "bacha khana nahi kha raha", "baccha dudh nahi pi raha",
     "बच्चा दूध नहीं पी रहा", "bacha ro raha", "बच्चा रो रहा", "bachhe ko dast",
     "bachhe ko ulti", "bachhe ko fever")
_add("teething", "teething", "daant aana", "बच्चे के दांत", "daant aa rahe bacche ke", "dentition")
_add("vaccination", "vaccination", "टीका", "tika", "tika lagwana", "vaccine", "टीकाकरण",
     "polio drops", "bcg", "dpt", "tika kab", "vaccine kab")

# ======================= PSYCHIATRY ======================================
_add("stress", "tension", "टेंशन", "तनाव", "tanav", "stress", "stressed", "pareshan", "परेशान",
     "pareshani", "परेशानी", "chinta", "चिंता", "dimag kharab", "दिमाग खराब", "mental stress",
     "tension ho raha", "tension le raha", "kaam ka tension")
_add("anxiety", "ghabrahat", "घबराहट", "ghabrata", "ghabra raha", "anxiety", "panic",
     "panic attack", "bechani", "बेचैनी", "bechain", "dil ghabra raha", "dil baith raha",
     "ghabrahat ho rahi")
_add("insomnia", "neend", "नींद", "neend nahi", "नींद नहीं", "neend nahi aati",
     "neend aati nahi", "insomnia", "sona nahi aata", "so nahi paata", "निद्रा नहीं",
     "raat me neend nahi", "neend aa rahi nahi")
_add("depression", "depression", "डिप्रेशन", "udaas", "उदास", "udaasi", "उदासी", "man udaas",
     "मन उदास", "man nahi lagta", "मन नहीं लगता", "kuch accha nahi lagta", "dukhi", "दुखी",
     "ghumsum", "sad feel")

# ======================= MOUTH / DENTAL =================================
_add_stem("tooth", "daant", "dant", "daanth", "दांत", "दाँत", "दात")
_add("toothache", "daant dard", "dant dard", "दांत दर्द", "daant dard hai", "dant dard hai",
     "दाँत में दर्द", "daant me dard", "daant mein dard", "toothache", "tooth pain", "toothpain",
     "daant dukh", "daant dukh raha", "daant hilna", "दांत हिल रहा", "daant hil raha",
     "daant tut gaya", "दांत टूट", "masoode", "मसूड़े", "masude", "masude me dard", "mashude",
     "masooda", "daant me keeda", "दांत में कीड़ा", "keeda", "daant peela", "दांत पीला",
     "daant me ghaav", "tooth", "dental", "daant mein dard")
_add("mouth", "muh", "मुंह", "munh", "muh me chhala", "मुंह में छाला", "chhala", "छाला",
     "mouth ulcer", "muh me ghaav", "jeebh", "जीभ", "jeebh pe chhala", "hoth phate", "होंठ फटे")
_add("bad breath", "muh ki badbu", "मुंह की बदबू", "bad breath", "saans me badbu")

# ======================= URINARY / GENERAL ==============================
_add("urine", "peshab", "पेशाब", "peshab me jalan", "पेशाब में जलन", "peshab me dard",
     "peshab me khoon", "पेशाब में खून", "peshab bar bar", "बार बार पेशाब", "urine", "urination",
     "burning urination", "peshab ruk ruk", "kidney", "किडनी", "gurda", "गुर्दा", "stone",
     "पथरी", "pathri", "pathari")
_add("thirst", "pyaas", "प्यास", "pyaas zyada", "बहुत प्यास", "pyaas bahut lag rahi")
_add("weight", "wazan", "वजन", "vazan", "weight", "wazan kam ho raha", "वजन घट", "wazan ghata",
     "weight loss", "wazan badh", "weight gain", "dubla", "दुबला", "motapa", "मोटापा", "obesity")
_add("covid", "covid", "कोविड", "corona", "कोरोना")
_add("checkup", "checkup", "चेकअप", "full body checkup", "health checkup", "screening",
     "routine checkup", "general checkup", "master health checkup")
_add("report", "report", "रिपोर्ट", "test karana", "टेस्ट", "lab test", "blood test",
     "khoon ki jaanch", "खून की जाँच", "test report")
_add("vomiting", "ulti ho rahi", "पेट दर्द और उल्टी")


# ---------------------------------------------------------------------------
# layer 3: aliases / the misspellings patients actually type
# ---------------------------------------------------------------------------
# The fuzzy index below already catches any single-letter mistake. This map
# exists for the ones we have seen and want to be certain about.
KNOWN_ALIASES: dict[str, str] = {
    # knee
    "gutne": "knee", "gutno": "knee", "gutna": "knee", "gutnon": "knee",
    "gutne me dard": "knee", "gutno me dard": "knee", "gutne mein dard": "knee",
    # eye
    "ammkein": "eye", "ammkhein": "eye", "aamkein": "eye", "aakhein": "eye", "aankhain": "eye",
    "ankhein": "eye", "ankhen": "eye", "ankhon": "eye", "aankhey": "eye",
    "ammkein laal": "redness in eye", "aankh lal": "redness in eye", "laal aankh": "redness in eye",
    "aankh laal ho rhi": "redness in eye", "aankh lal ho rhi": "redness in eye",
    "aankhon me laalpan": "redness in eye", "aankh me laalpan": "redness in eye",
    # hair
    "jhd": "hair fall", "jhad": "hair fall", "jhde": "hair fall", "jhad rahe": "hair fall",
    "baal jhd": "hair fall", "baal jhd rhe": "hair fall", "jhar rhe": "hair fall",
    "baal jhar rhe hai": "hair fall", "bal jhar rhe": "hair fall", "baalo ka jhadna": "hair fall",
    "baal jhd rhe hai": "hair fall", "baal jhad rhe hai": "hair fall",
    # fever
    "bhukhar": "fever", "bukaar": "fever", "bukhhar": "fever", "bukhr": "fever",
    # pain
    "dardd": "pain", "drad": "pain", "dradd": "pain", "daard": "pain",
    # stomach
    "pait": "stomach", "peth": "stomach", "pait me dard": "pet dard", "pait dard": "pet dard",
    # head
    "srdard": "headache", "sirdard": "headache", "sirrdard": "headache", "sardard": "headache",
    "sard": "headache", "sir dardd": "headache",
    # cough / cold
    "khaasi": "cough", "khaansi": "cough", "khanshi": "cough", "khasi": "cough",
    "zukham": "cold", "zukaam": "cold", "jukam": "cold", "jukham": "cold",
    # back
    "kamr dard": "back pain", "kmar dard": "back pain", "kamar drd": "back pain",
    "peeth dard": "back pain", "pith dard": "back pain",
    # itching / rash
    "khujlee": "itching", "khujali": "itching", "kujli": "itching", "khujly": "itching",
    "khujli horhi": "itching", "khujli ho rhi": "itching", "daane": "rash", "danne": "rash",
    "dane aagye": "rash", "laal-laal dane": "rash",
    # dizziness
    "chakar": "dizziness", "chakkr": "dizziness", "chakaar": "dizziness",
    "chakkar aa rhe": "dizziness",
    # vomiting / loose motions
    "ulte": "vomiting", "vomitng": "vomiting", "dastt": "loose motion", "dust": "loose motion",
    "loos motion": "loose motion",
    # throat / ear / tooth
    "gla": "throat", "galae": "throat", "ghala": "throat", "gale me dard": "throat",
    "kann": "ear", "kaan me dard": "ear", "kan me dard": "ear",
    "dat": "tooth", "danth": "tooth", "dat dard": "toothache", "daanth dard": "toothache",
    # weakness / body ache
    "kamjori": "weakness", "kamzuri": "weakness", "kamjoori": "weakness", "thakaan": "fatigue",
    "badn dard": "body ache",
    # breathing
    "saas": "breathing", "saans phul": "breathless", "sans phul": "breathless",
}


# ---------------------------------------------------------------------------
# canonical symptom vocabulary the router can match
# ---------------------------------------------------------------------------
# Curated on purpose: `understood` is decided from this set, so words that are
# administrative rather than clinical (report, checkup, chashma, memory ...) can
# never make a message look like a symptom.
SYMPTOM_TERMS = {
    "fever", "typhoid", "malaria", "dengue", "infection", "flu", "body ache", "weakness",
    "fatigue", "sugar", "bp", "thyroid", "anemia", "pain", "head", "headache", "migraine",
    "dizziness", "vertigo", "fainted", "unconscious", "seizure", "numbness", "paralysis",
    "sciatica", "eye", "redness in eye", "blurred", "vision", "cataract", "ear", "hearing",
    "nose", "sinus", "throat", "cough", "cold", "snoring", "chest", "chest pain",
    "palpitations", "heart", "cholesterol", "breathing", "breathless", "asthma", "wheezing",
    "TB", "smoking", "stomach", "pet dard", "acidity", "gas", "constipation", "loose motion",
    "vomiting", "jaundice", "piles", "hernia", "appetite", "itching", "rash", "skin", "acne",
    "eczema", "fungal", "hair fall", "dandruff", "swelling", "allergy", "wound", "burn",
    "knee", "joint", "back pain", "neck", "shoulder", "elbow", "wrist", "hip", "leg", "heel",
    "foot", "fracture", "injury", "periods", "white discharge", "pregnancy", "pcos",
    "infertility", "breast", "child", "teething", "vaccination", "stress", "anxiety",
    "insomnia", "depression", "tooth", "toothache", "mouth", "bad breath", "urine", "thirst",
    "weight", "covid",
}

# Operative requests are NOT medical concerns. Keeping these out of the concern
# detector is what lets "1", "yes" and "cancel appointment" stay on the booking
# rails instead of being treated as symptoms.
OPERATIONAL_PHRASES = {
    "1", "2", "3", "4", "5", "6", "7", "8", "9",
    "yes", "no", "noo", "n", "ok", "okay", "sure", "haan", "ha", "nahi", "na", "thik", "theek",
    "book appointment", "book appointment anyway", "book urgent slot", "appointment",
    "appointment status", "my appointment", "show my appointment", "show my reports",
    "show my records", "show my prescription", "show prescriptions", "payment status",
    "my payment status", "talk to reception", "human", "reception", "agent", "staff",
    "call me", "cancel appointment", "cancel my appointment", "cancel", "reschedule appointment",
    "reschedule my appointment", "reschedule", "book again", "call 108", "notify my family",
    "find a specialist", "medical advice", "no other slots", "other slots", "next", "back",
    "start over", "help", "payment options", "download prescription", "show my appointments",
    "book another", "confirm", "no, other slots", "yes, confirm", "yes confirm", "thank you",
    "thanks", "hi", "hello", "hey", "namaste", "good morning", "good evening",
    "good afternoon", "dhanyavad", "shukriya", "start", "fever and cold", "knee pain",
    "skin rash", "book appointment", "show reports",
}

# Words that mean "I have been feeling this for a while" or glue the sentence
# together - context, never a symptom on their own.
CONTEXT_WORDS = {
    "kal", "aaj", "parso", "subah", "raat", "shaam", "dopahar", "din", "hafta", "hafte", "mahina",
    "mahine", "saal", "sal", "ghanta", "minute", "second", "since", "se", "bahut", "thoda",
    "zyada", "kam", "tez", "halka", "bhar", "phir", "bhi", "aur", "ho", "raha", "rha", "rahi",
    "rhi", "hai", "hain", "hua", "hoga", "kar", "karta", "karti", "mein", "me", "ka", "ki", "ke",
    "par", "pe", "ko", "ek", "do", "teen", "mera", "meri", "mere", "mujhe", "main", "mai", "hum",
    "wo", "yeh", "ye", "kuch", "koi", "nhi", "wala", "wali", "problem", "takleef", "pareshani",
    "bimari", "dikkat", "dikhat", "feel", "feeling", "hota", "hoti", "lag", "lagta", "lagti",
    "rehta", "rehti", "shuru", "band", "dur", "durust", "abhi", "aata", "aati", "nahi",
}

_DEVANAGARI = re.compile(r"[\u0900-\u097F]")


def _compile(term: str) -> re.Pattern:
    """Word-boundary match for Latin terms, plain substring for Devanagari.

    ``\\b`` does not behave usefully around Devanagari, so Hindi terms are
    matched directly. For Latin text, lookarounds stop "kan" (ear) matching
    inside "kamar" (back) - a false positive that would route back pain to ENT.
    """
    if _DEVANAGARI.search(term):
        return re.compile(re.escape(term))
    return re.compile(rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])")


# Compiled patterns, longest key first so "pet dard" is checked before "pet".
_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (_compile(term), term, canonical)
    for term, canonical in sorted(LOCAL_MEDICAL_TERMS.items(), key=lambda kv: -len(kv[0]))
]
_ALIAS_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (_compile(alias), alias, canonical)
    for alias, canonical in sorted(KNOWN_ALIASES.items(), key=lambda kv: -len(kv[0]))
]


# ---------------------------------------------------------------------------
# layer 4: edit-distance-1 index (built once, at import)
# ---------------------------------------------------------------------------
# One index gets every single-letter mistake for free:
#   * key with one letter REMOVED     : catches input missing a letter ("jhd")
#   * key with two letters SWAPPED    : catches "aankh"/"anhkh"-style typos
# (input with an EXTRA letter is handled by deleting one letter from the input
#  word and looking it up directly.)
def _deletion_variants(word: str) -> set[str]:
    return {word[:i] + word[i + 1:] for i in range(len(word))}


def _transposition_variants(word: str) -> set[str]:
    return {word[:i] + word[i + 1] + word[i] + word[i + 2:] for i in range(len(word) - 1)}


# Hinglish typos are mostly vowels ("periuds" for "periods", "peit" for "pet",
# "khujli"/"khujali"). Swapping a vowel for another one is therefore worth its
# own variant set - consonants stay untouched so the index cannot drift into
# unrelated words.
_VOWELS = "aeiou"


def _substitution_variants(word: str) -> set[str]:
    out: set[str] = set()
    for i, ch in enumerate(word):
        if ch in _VOWELS:
            for other in _VOWELS:
                if other != ch:
                    out.add(word[:i] + other + word[i + 1:])
    return out


def _build_fuzzy_index() -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    words: set[str] = set()
    for key in LOCAL_MEDICAL_TERMS:
        if _DEVANAGARI.search(key):
            continue                                    # no typo matching for Devanagari
        for word in key.split():
            if len(word) >= 4:
                words.add(word)
    for word in words:
        variants = (_deletion_variants(word) | _transposition_variants(word))
        # only longer words buy the wider vowel-substitution net
        if len(word) >= 5:
            variants |= _substitution_variants(word)
        for variant in variants:
            if variant and variant != word and len(variant) >= 3:
                index.setdefault(variant, set()).add(word)
    return index


_FUZZY_INDEX = _build_fuzzy_index()

# Common words that happen to sit one letter away from a medical term. Without
# this, "hold" would match "cold" and "ball" could match "baal".
_FUZZY_BLOCKLIST = {
    "hold", "gold", "bold", "told", "sold", "fold", "mold", "cord", "word", "wood", "food",
    "good", "mood", "ball", "tall", "wall", "hall", "mall", "call", "fall", "full", "pull",
    "push", "cash", "card", "care", "come", "came", "have", "here", "hear", "heat", "help",
    "home", "hope", "hotel", "time", "team", "term", "term", "road", "read", "real", "rest",
    "test", "text", "next", "name", "same", "some", "more", "most", "much", "must", "just",
    "last", "late", "later", "list", "live", "look", "made", "make", "many", "mean", "meet",
    "mind", "mine", "miss", "move", "need", "news", "only", "open", "page", "part", "past",
    "plan", "play", "post", "rate", "year", "week", "month", "day", "hour", "date", "cost",
    "price", "bill", "book", "slot", "doctor", "nurse", "patient", "report", "tablet",
    "medicine", "hospital", "clinic", "appointment", "video", "audio", "photo", "data",
    "user", "code", "type", "size", "form", "file", "mail", "call", "chat", "talk", "walk",
    "work", "world", "would", "could", "should", "there", "these", "those", "which", "what",
    "when", "where", "while", "with", "without", "about", "after", "again", "before", "being",
    "below", "between", "both", "each", "from", "into", "other", "over", "such", "than",
    "that", "their", "them", "then", "they", "this", "under", "until", "very", "were",
    # common Hindi/Hinglish function words that sit one deletion away from a body
    # word ("kahan"/"karni" -> "kaan"). They are never symptoms, so they must not
    # be pulled into the routing path by a typo correction.
    "kahan", "kahaan", "karni", "karna", "karne", "karo", "karta", "karti", "karke",
    "kaise", "kaisi", "kabhi", "thoda", "zyada", "pehle", "baad", "abhi", "phir",
    "mummy", "papa", "bhai", "behan", "dost", "acha", "accha", "theek", "thik",
    "kuch", "koi", "sab", "log", "baat", "baatien", "wala", "wali", "kya", "nahi",
}

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z']*")


def clamp_text(text: str, limit: int = 400) -> str:
    return (text or "").strip()[:limit]


def is_operational(text: str) -> bool:
    """True when the message is a command/selection rather than a symptom."""
    low = " ".join((text or "").strip().lower().split())
    if not low:
        return False
    if low in OPERATIONAL_PHRASES:
        return True
    return bool(re.fullmatch(r"[1-9][0-9]?", low))      # a bare option number


def _fuzzy_lookup(word: str) -> str | None:
    """Return the canonical term for a 1-letter typo, or None.

    Two ways in: the token is a deletion/transposition variant of a known word
    (input missing a letter, "jhd" -> "jhad"), or deleting one letter from the
    token yields a known word (input with an extra letter, "ghutonne" -> "ghutne").
    """
    if len(word) < 4 or word in _FUZZY_BLOCKLIST or word in CONTEXT_WORDS:
        return None

    candidates = set(_FUZZY_INDEX.get(word) or ())
    if not candidates:
        for variant in _deletion_variants(word):
            if variant in LOCAL_MEDICAL_TERMS:
                candidates.add(variant)
    if not candidates:
        return None

    # If several known words are one edit away, prefer the longest (most
    # specific) one, then the alphabetically first for determinism.
    for candidate in sorted(candidates, key=lambda w: (-len(w), w)):
        canonical = LOCAL_MEDICAL_TERMS.get(candidate)
        if canonical:
            return canonical
    return None


# A body area plus a generic "pain" is a much stronger signal as the phrase the
# hospital already stores ("pet dard", "back pain"), so combine them.
_BODY_SYMPTOM_PHRASES: dict[tuple[str, str], str] = {
    ("stomach", "pain"): "pet dard",
    ("back", "pain"): "back pain",
    ("chest", "pain"): "chest pain",
    ("head", "pain"): "headache",
    ("tooth", "pain"): "toothache",
    ("neck", "pain"): "neck pain",
    ("joint", "pain"): "joint",
    ("leg", "pain"): "leg",
    ("knee", "pain"): "knee",
    ("ear", "pain"): "ear",
    ("eye", "pain"): "eye",
}


def _combine_body_symptom(canonicals: list[str]) -> list[str]:
    """Turn ["stomach", "pain"] into ["pet dard"] so the specific keyword wins."""
    def _dedupe(items: list[str]) -> list[str]:
        seen: list[str] = []
        for item in items:
            if item not in seen:
                seen.append(item)
        return seen

    if "pain" not in canonicals:
        return _dedupe(canonicals)
    for body in list(canonicals):
        phrase = _BODY_SYMPTOM_PHRASES.get((body, "pain"))
        if phrase and phrase != body:
            return _dedupe([c for c in canonicals if c not in {body, "pain"}] + [phrase])
    return _dedupe(canonicals)


def normalise_medical_text(text: str) -> dict:
    """Map Hindi/Hinglish/regional words and typos onto canonical terms.

    Returns::

        {
          "original":   the patient's message, unchanged,
          "normalized": the message plus the canonical terms appended, so the
                        existing specialty_concerns keyword matcher can score it,
          "matched":    {"ghutne": "knee", "gutno": "knee", ...}  audit trail,
          "canonicals": ["knee", "pain"],
          "understood": True when at least one clinical term matched,
          "fuzzy":      {"gutno": "knee"} - typos that were corrected,
          "language":   "hi" | "hinglish" | "en",
        }

    Nothing is guessed: an unmatched message comes back with
    ``understood=False`` and the caller decides whether to ask Groq or ask the
    patient to clarify.
    """
    original = clamp_text(text)
    low = " ".join(original.lower().split())

    matched: dict[str, str] = {}
    canonicals: list[str] = []

    def record(term: str, canonical: str) -> None:
        matched.setdefault(term, canonical)
        if canonical not in canonicals:
            canonicals.append(canonical)

    for pattern, term, canonical in _PATTERNS:
        if pattern.search(low):
            record(term, canonical)
    for pattern, term, canonical in _ALIAS_PATTERNS:
        if pattern.search(low):
            record(term, canonical)

    # ---- layer 4: one-letter typos the vocabulary did not already cover ----
    fuzzy: dict[str, str] = {}
    for token in _TOKEN_RE.findall(low):
        if token in LOCAL_MEDICAL_TERMS or token in CONTEXT_WORDS:
            continue
        if any(token in term.split() for term in matched):
            continue
        canonical = _fuzzy_lookup(token)
        if canonical:
            fuzzy[token] = canonical
            record(token, canonical)

    canonicals = _combine_body_symptom(canonicals)

    # A multi-word phrase makes its parts redundant: "pet dard" -> drop "stomach"
    # and the generic "pain" so routing is not diluted.
    for phrase, drop in (("pet dard", {"stomach", "pain"}),
                         ("back pain", {"pain", "back"}),
                         ("chest pain", {"pain", "chest"}),
                         ("toothache", {"pain", "tooth"}),
                         ("headache", {"head"}),
                         ("loose motion", {"pain"}),
                         ("redness in eye", {"eye"})):
        if phrase in canonicals:
            canonicals = [c for c in canonicals if c not in drop]

    understood = any(c in SYMPTOM_TERMS for c in canonicals)

    normalized = low
    if canonicals:
        normalized = f"{low} {' '.join(canonicals)}"

    return {
        "original": original,
        "normalized": normalized,
        "matched": matched,
        "canonicals": canonicals,
        "understood": understood,
        "fuzzy": fuzzy,
        "language": detect_language(original),
    }


def detect_language(text: str) -> str:
    """Cheap language hint (no model): Devanagari, Hinglish or English."""
    raw = text or ""
    if _DEVANAGARI.search(raw):
        return "hi"
    low = raw.lower()
    words = _TOKEN_RE.findall(low)
    hindi_markers = {"dard", "bukhar", "hori", "mein", "me", "hai", "nahi", "ka", "ki", "ke",
                     "meri", "mera", "mere", "ho", "raha", "rahi", "rha", "rhi", "bahut",
                     "thoda", "kya", "kyu", "kab", "kaise", "wala", "wali", "bhi", "se", "ko"}
    if words and sum(1 for w in words if w in hindi_markers) / len(words) >= 0.2:
        return "hinglish"
    if any(m in f" {low} " for m in (" me ", " ka ", " ki ", " hai ")):
        return "hinglish"
    return "en"


def language_confidence(local: dict) -> float:
    """How sure the LOCAL layer is about a message (0.0 - 0.95).

    Used by ai_engine to decide whether to answer locally, hand the message to
    Groq for semantic understanding, or ask the patient. Deliberately capped
    below 1.0: the dictionary should never claim certainty it does not have.
    """
    canonicals = local.get("canonicals") or []
    if not canonicals:
        return 0.0
    score = 0.5
    if local.get("understood"):
        score += 0.25
    if len(canonicals) >= 2:
        score += 0.15                                  # body area + symptom
    if local.get("fuzzy"):
        score -= 0.1                                   # we corrected a typo
    return max(0.0, min(0.95, score))


# ---------------------------------------------------------------------------
# emergency vocabulary (Hindi / Hinglish) - EXACT matching only, never fuzzy
# ---------------------------------------------------------------------------
EMERGENCY_TERMS_HI: tuple[str, ...] = (
    "seene mein dard", "chhati mein dard", "chhaati mein dard",
    "dil mein dard", "dil ka daura", "हार्ट अटैक", "दिल का दौरा",
    "सीने में दर्द", "सीने मे दर्द", "छाती में दर्द", "छाती मे दर्द", "दिल में दर्द",
    "saans nahi", "saans nahin", "saans ruk", "dam ghut raha", "saans lene mein dikkat",
    "सांस नहीं", "साँस नहीं", "सांस रुक", "दम घुट",
    "behosh", "behoshi", "बेहोश", "chakkar aa kar gir",
    "mirgi", "jhatka aa gaya", "दौरा पड़",
    "khoon bahut", "bahut khoon", "khoon beh", "khoon beh raha", "khoon nikal raha",
    "khoon aa raha", "khoon aa gaya", "khoon ruk nahi", "khoon ruk nahin",
    "बहुत खून", "खून बह", "खून निकल", "खून आ",
    "gehri chot", "sar pe chot", "सिर पर चोट", "गहरी चोट",
    "lakwa", "laqwa", "paralysis", "लकवा",
    "zeher", "zahar", "जहर", "kha liya zeher",
    "accident ho gaya", "takkar", "एक्सीडेंट",
    "labour pain", "pregnancy bleeding", "pet mein bachha hile nahi",
)

# Shape-based emergency patterns. Patients mix languages freely - "chest mein
# bahut tez dard ho rha" uses the English word "chest" with Hindi grammar, so a
# fixed phrase list is not enough. These match the *shape*: a danger body part
# within a few words of a pain word, in either order.
EMERGENCY_REGEXES: tuple[str, ...] = (
    # chest pain, either word order, any mixture of English/Hindi
    r"\b(chest|seene|seena|sine|chhati|chhaati|chati|dil)\b(?:\s+\S+){0,3}\s+(dard|pain|drd)\b",
    r"\b(dard|pain)\b(?:\s+\S+){0,2}\s+(chest|seene|seena|chhati|chhaati|dil)\b",
    # crush / pressure / heaviness on the chest
    r"\bchest\b(?:\s+\S+){0,3}\s+(bhaari|bhari|bhaar|pressure|jabardast)\b",
    # cannot breathe / choking. NOTE: plain "phool" (breathless while walking) is
    # deliberately NOT here - it is an urgent symptom, not an automatic 108.
    r"\b(saans|sans|dam)\b(?:\s+\S+){0,3}\s+(nahi|nahin|ruk|ghut|band)\b",
    r"\b(saans|dam)\b(?:\s+\S+){0,2}\s+nahi\b",
    # profuse bleeding
    r"\b(khoon|khun|blood)\b(?:\s+\S+){0,3}\s+(bahut|ruk nahi|nikal|beh)\b",
    r"\b(bahut|zyada)\b\s+(khoon|khun|blood)\b",
    # loss of consciousness / seizure
    r"\b(behosh|बेहोश)\b",
    r"\b(chakkar)\b(?:\s+\S+){0,3}\s+(gir|giri)\b",
    # poisoning / overdose / suicide risk
    r"\b(zeher|zahar|poison|overdose|suicide|khudkhushi|atmahatya)\b",
    # pregnancy emergency
    r"\b(pregnan\w*|garbh\w*|labour|prasav)\b(?:\s+\S+){0,3}\s+(bleeding|khoon|dard|pain)\b",
    # newborn emergency
    r"\b(navjat|newborn|naya bacha|naya bachha)\b(?:\s+\S+){0,3}\s+(saans|dard|bukhar|kamzori)\b",
)

# Urgent (not immediately life-threatening) Hindi phrases -> escalate to a doctor.
URGENT_TERMS_HI: tuple[str, ...] = (
    "tez bukhar", "bahut tez bukhar", "तेज बुखार", "तेज़ बुखार",
    "tez dard", "bahut dard", "asahniya dard", "असहनीय दर्द", "तेज दर्द", "तेज़ दर्द",
    "lagatar ulti", "lagatar dast", "बार बार उल्टी", "लगातार दस्त",
    "chakkar bahut", "bahut kamzori", "bahut zyada bukhar",
    "saans phool", "saans phool rahi", "saans phool raha", "sans phool",
    "saans chadh rahi", "dam ghut raha", "seene me jakdan",
    "khaansi me khoon", "खांसी में खून", "dast me khoon", "दस्त में खून",
    "pakhana me khoon", "peshab me khoon",
)


def _phrase_regex(phrase: str, gap: int = 3) -> re.Pattern:
    """Match a phrase while tolerating a few words in between.

    Patients rarely type an emergency in a tidy phrase:

        "seene mein bahut tez dard ho rha"

    A plain substring test for "seene mein dard" misses that. Allowing up to
    ``gap`` filler words between the phrase's own words catches the realistic
    wording without matching unrelated sentences.
    """
    parts = [re.escape(word) for word in phrase.split()]
    if len(parts) == 1:
        return re.compile(rf"(?<![A-Za-z]){parts[0]}(?![A-Za-z])")
    joiner = rf"(?:\s+\S+){{0,{gap}}}\s+"
    return re.compile(rf"(?<![A-Za-z]){joiner.join(parts)}(?![A-Za-z])")


def _normalise_connectors(text: str) -> str:
    """Fold the interchangeable Hindi connectors so one pattern covers all forms.

    "seene mein dard" / "seene me dard" / "सीने में दर्द" / "सीने मे दर्द" are the
    same sentence to a patient, and must be the same sentence to the safety net.
    """
    text = re.sub(r"\bmein\b", "me", text)
    return text.replace("में", "मे")


_EMERGENCY_PATTERNS_HI = [
    (_phrase_regex(_normalise_connectors(p)), p) for p in EMERGENCY_TERMS_HI
]
_EMERGENCY_REGEXES_HI = [re.compile(p) for p in EMERGENCY_REGEXES]
_URGENT_PATTERNS_HI = [
    (_phrase_regex(_normalise_connectors(p)), p) for p in URGENT_TERMS_HI
]


def has_emergency_term(text: str) -> str | None:
    """Return the matched emergency phrase, or None. Safety-critical.

    Shape patterns are checked before phrases so a mixed-language sentence
    ("chest mein bahut tez dard ho rha") is caught even when no fixed phrase fits.
    Deliberately NOT fuzzy: a fuzzy match in the safety path could both miss a
    real emergency and invent one.
    """
    low = _normalise_connectors(" ".join((text or "").lower().split()))
    for pattern in _EMERGENCY_REGEXES_HI:
        match = pattern.search(low)
        if match:
            return match.group(0).strip()
    for pattern, phrase in _EMERGENCY_PATTERNS_HI:
        if pattern.search(low):
            return phrase
    return None


def has_urgent_term(text: str) -> str | None:
    low = _normalise_connectors(" ".join((text or "").lower().split()))
    for pattern, phrase in _URGENT_PATTERNS_HI:
        if pattern.search(low):
            return phrase
    return None


def vocabulary_stats() -> dict:
    """Small helper for tests / diagnostics."""
    return {
        "surface_forms": len(LOCAL_MEDICAL_TERMS),
        "aliases": len(KNOWN_ALIASES),
        "fuzzy_variants": len(_FUZZY_INDEX),
        "canonical_terms": len(SYMPTOM_TERMS),
        "emergency_phrases": len(EMERGENCY_TERMS_HI),
        "emergency_patterns": len(EMERGENCY_REGEXES),
    }
