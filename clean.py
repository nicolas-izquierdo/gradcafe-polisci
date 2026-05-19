"""School name normalization and decision class cleaning."""

import re

# (pattern, canonical_name, usnwr_rank)
SCHOOL_RULES = [
    # Rank 1
    (r"harvard", "Harvard University", 1),
    # Rank 2
    (r"princeton", "Princeton University", 2),
    # Rank 3
    (r"stanford", "Stanford University", 3),
    # Rank 4
    (r"mit\b|mass(achusetts)? inst(itute)? of tech", "MIT", 4),
    # Rank 5
    (r"michigan\b.*ann arbor|university of michigan", "University of Michigan", 5),
    # Rank 6
    (r"\bucla\b|univ.*california.*los angeles|los angeles.*california", "UCLA", 6),
    # Rank 7
    (r"\buc san diego\b|\bucsd\b|univ.*california.*san diego", "UC San Diego", 7),
    # Rank 8
    (r"duke\b", "Duke University", 8),
    # Rank 9
    (r"columbia\b", "Columbia University", 9),
    # Rank 10
    (r"yale\b", "Yale University", 10),
    # Rank 11
    (r"university of chicago|\buchicago\b", "University of Chicago", 11),
    # Rank 12
    (r"ohio state|osu\b.*polisci|ohio st\b", "Ohio State University", 12),
    # Rank 13
    (r"rochester\b", "University of Rochester", 13),
    # Rank 14
    (r"wash(ington)? univ(ersity)? st\.? louis|wustl", "Washington University in St. Louis", 14),
    # Rank 15
    (r"nyu\b|new york univ", "New York University", 15),
    # Rank 16
    (r"uc berkeley|\bberkeley\b|univ.*california.*berkeley", "UC Berkeley", 16),
    # Rank 17
    (r"penn\b|university of pennsylvania|upenn", "University of Pennsylvania", 17),
    # Rank 18
    (r"cornell\b", "Cornell University", 18),
    # Rank 19
    (r"northwestern\b", "Northwestern University", 19),
    # Rank 20
    (r"unc\b.*chapel|university of north carolina.*chapel|chapel hill|university of north carolina\b(?!.*state|.*central|.*charlotte|.*greensboro|.*asheville|.*wilmington|.*pembroke)|\bunc\b(?!.*state|.*central|.*charlotte)", "UNC Chapel Hill", 20),
    # Rank 21
    (r"wisconsin\b.*madison|university of wisconsin", "University of Wisconsin-Madison", 21),
    # Rank 22
    (r"minnesota\b", "University of Minnesota", 22),
    # Rank 23
    (r"indiana univ|iu\b.*bloomington|bloomington.*indiana", "Indiana University", 23),
    # Rank 24
    (r"emory\b", "Emory University", 24),
    # Rank 25
    (r"vanderbilt\b", "Vanderbilt University", 25),
    # Rank 26
    (r"rice\b", "Rice University", 26),
    # Rank 27
    (r"usc\b|university of southern california", "University of Southern California", 27),
    # Rank 28
    (r"texas\b.*austin|ut austin|univ.*texas.*austin", "University of Texas at Austin", 28),
    # Rank 29
    (r"notre dame\b", "University of Notre Dame", 29),
    # Rank 30
    (r"illinois\b.*urbana|uiuc|univ.*illinois.*champaign", "University of Illinois Urbana-Champaign", 30),
    # Rank 31
    (r"georgetown\b", "Georgetown University", 31),
    # Rank 32
    (r"uc davis\b|univ.*california.*davis", "UC Davis", 32),
    # Rank 33
    (r"uc santa barbara|\bucsb\b|univ.*california.*santa barbara", "UC Santa Barbara", 33),
    # Rank 34
    (r"florida\b.*gainesville|university of florida", "University of Florida", 34),
    # Rank 35
    (r"iowa\b", "University of Iowa", 35),
    # Rank 36
    (r"michigan state\b|msu\b.*east lansing|east lansing", "Michigan State University", 36),
    # Rank 37
    (r"penn state\b|pennsylvania state|psu\b", "Penn State University", 37),
    # Rank 38
    (r"purdue\b", "Purdue University", 38),
    # Rank 39
    (r"arizona state\b|asu\b.*tempe", "Arizona State University", 39),
    # Rank 40
    (r"university of arizona\b", "University of Arizona", 40),
    # Rank 41
    (r"george washington\b|\bgwu\b", "George Washington University", 41),
    # Rank 42
    (r"american univ.*washington|american university", "American University", 42),
    # Rank 43
    (r"boston univ\b|\bbu\b.*political|boston university", "Boston University", 43),
    # Rank 44
    (r"boston college\b|\bbc\b.*chestnut", "Boston College", 44),
    # Rank 45
    (r"tufts\b", "Tufts University", 45),
    # Rank 46
    (r"northeastern\b.*boston", "Northeastern University", 46),
    # Rank 47
    (r"rutgers\b", "Rutgers University", 47),
    # Rank 48
    (r"johns hopkins\b|\bjhu\b", "Johns Hopkins University", 48),
    # Rank 49
    (r"uc irvine\b|\buci\b|univ.*california.*irvine", "UC Irvine", 49),
    # Rank 50
    (r"stony brook\b|suny stony|state univ.*new york.*stony", "Stony Brook University", 50),
    # Rank 51
    (r"binghamton\b|suny binghamton", "SUNY Binghamton", 51),
    # Rank 52
    (r"pitt\b|university of pittsburgh|pittsburgh\b", "University of Pittsburgh", 52),
    # Rank 53
    (r"university of maryland\b|umd\b.*college park", "University of Maryland", 53),
    # Rank 54
    (r"virginia\b.*charlottesville|university of virginia|\buva\b", "University of Virginia", 54),
    # Rank 55
    (r"north carolina state|nc state\b", "NC State University", 55),
    # Rank 56
    (r"colorado\b.*boulder|university of colorado", "University of Colorado Boulder", 56),
    # Rank 57
    (r"tulane\b", "Tulane University", 57),
    # Rank 58
    (r"case western\b", "Case Western Reserve University", 58),
    # Rank 59
    (r"lsu\b|louisiana state", "Louisiana State University", 59),
    # Rank 60
    (r"florida state\b|\bfsu\b", "Florida State University", 60),
    # Additional programs (unranked / international / common in polisci PhD data)
    (r"brown univ|brown\b.*providence|\bbrownuniv", "Brown University", 61),
    (r"university of toronto|\butoront|\buoft\b|u of t\b", "University of Toronto", 62),
    (r"university of washington\b|\buw\b.*seattle|seattle.*washington", "University of Washington", 63),
    (r"syracuse\b", "Syracuse University", 64),
    (r"umass\b.*amherst|university of massachusetts.*amherst|massachusetts.*amherst", "UMass Amherst", 65),
    (r"mcgill\b", "McGill University", 66),
    (r"cuny graduate|city univ.*new york.*graduate|graduate center.*cuny", "CUNY Graduate Center", 67),
    (r"\bcuny\b|city university of new york", "CUNY", 68),
    (r"london school of economics|\blse\b", "London School of Economics", 69),
    (r"university of connecticut|\buconn\b", "University of Connecticut", 70),
    (r"university of georgia|\buga\b(?!.*state)", "University of Georgia", 71),
    (r"texas a&m|tamu\b|texas a and m|texas aggie", "Texas A&M University", 72),
    (r"university of oregon\b|\buoregon\b", "University of Oregon", 73),
    (r"george mason\b|\bgmu\b", "George Mason University", 74),
    (r"temple univ\b", "Temple University", 75),
    (r"suny albany|university at albany", "SUNY Albany", 76),
    (r"uc riverside|\bucr\b|univ.*california.*riverside|california.*riverside\b", "UC Riverside", 77),
    (r"uc santa cruz|\bucsc\b|univ.*california.*santa cruz|california.*santa cruz", "UC Santa Cruz", 78),
    (r"uc merced|univ.*california.*merced|california.*merced", "UC Merced", 79),
    (r"york univ\b|york university", "York University", 80),
    (r"brandeis\b", "Brandeis University", 81),
    (r"central european univ|\bceu\b", "Central European University", 82),
    (r"claremont graduate", "Claremont Graduate University", 83),
    (r"university of delaware\b|\budel\b", "University of Delaware", 84),
    (r"university of houston\b", "University of Houston", 85),
    (r"the new school\b|newschool\b", "The New School", 86),
    (r"baylor\b", "Baylor University", 87),
    (r"university of south carolina\b(?!.*upstate)", "University of South Carolina", 88),
    (r"university of kansas\b|\bku\b.*lawrence", "University of Kansas", 89),
    (r"university of kentucky\b|\buk\b.*lexington", "University of Kentucky", 90),
    (r"university of missouri\b|mizzou\b", "University of Missouri", 91),
    (r"university of nebraska\b.*lincoln|unl\b", "University of Nebraska-Lincoln", 92),
    (r"university of oklahoma\b|\bou\b.*norman", "University of Oklahoma", 93),
    (r"university of tennessee\b", "University of Tennessee", 94),
    (r"university of utah\b", "University of Utah", 95),
    (r"virginia tech\b|virginia polytechnic", "Virginia Tech", 96),
    (r"washington state\b|wsu\b.*pullman", "Washington State University", 97),
    (r"wayne state\b", "Wayne State University", 98),
    (r"university of new mexico\b|\bunm\b", "University of New Mexico", 99),
    (r"university of miami\b(?!.*ohio)", "University of Miami", 100),
    (r"miami university\b.*ohio|miami.*ohio\b", "Miami University (OH)", 101),
    (r"fordham\b", "Fordham University", 102),
    (r"loyola\b.*chicago", "Loyola University Chicago", 103),
    (r"drexel\b", "Drexel University", 104),
    (r"american univ.*beirut|\baub\b", "American University of Beirut", 105),
    (r"queens univ\b|queen's univ", "Queen's University", 106),
    (r"western univ\b|university of western ontario", "Western University", 107),
    (r"university of alberta\b", "University of Alberta", 108),
    (r"university of british columbia\b|\bubc\b", "University of British Columbia", 109),
    (r"university of calgary\b", "University of Calgary", 110),
    (r"carleton univ\b", "Carleton University", 111),
    (r"dalhousie\b", "Dalhousie University", 112),
    (r"university of oxford\b|\boxford\b", "University of Oxford", 113),
    (r"university of cambridge\b|\bcambridge\b(?!.*ohio|.*mass)", "University of Cambridge", 114),
    (r"kings college london\b|\bkcl\b|king's college london", "King's College London", 115),
    (r"university college london\b|\bucl\b(?!.*irvine)", "University College London", 116),
    (r"university of edinburgh\b", "University of Edinburgh", 117),
    (r"university of amsterdam\b|\buva\b.*amsterdam", "University of Amsterdam", 118),
]

_COMPILED = [(re.compile(pat, re.IGNORECASE), name, rank) for pat, name, rank in SCHOOL_RULES]


def clean_school(raw: str) -> tuple[str, int | None]:
    """Return (canonical_name, usnwr_rank). Rank is None if unmatched."""
    if not raw or not isinstance(raw, str):
        return raw, None
    s = raw.strip()
    for pattern, name, rank in _COMPILED:
        if pattern.search(s):
            return name, rank
    return s, None


DECISION_MAP = {
    # Accepted variants
    "accepted": "Accepted",
    "accept": "Accepted",
    "admission": "Accepted",
    "admitted": "Accepted",
    "offer": "Accepted",
    # Rejected variants
    "rejected": "Rejected",
    "reject": "Rejected",
    "denial": "Rejected",
    "denied": "Rejected",
    # Waitlisted
    "waitlist": "Waitlisted",
    "wait list": "Waitlisted",
    "wait-list": "Waitlisted",
    "wl": "Waitlisted",
    # Interview
    "interview": "Interview",
    # Other
    "pending": "Other",
    "unknown": "Other",
    "other": "Other",
}


def normalize_decision(raw: str) -> str:
    if not raw or not isinstance(raw, str):
        return "Other"
    lower = raw.strip().lower()
    for key, val in DECISION_MAP.items():
        if key in lower:
            return val
    return "Other"


def normalize_degree(raw: str) -> str:
    if not raw or not isinstance(raw, str):
        return "Other"
    lower = raw.strip().lower()
    if "phd" in lower or "ph.d" in lower or "doctoral" in lower or "doctor" in lower:
        return "PhD"
    if "master" in lower or "\bma\b" in lower or "m.a." in lower or "ms\b" in lower or "m.s." in lower:
        return "MA"
    # regex check for standalone MA/MS
    if re.search(r'\bm\.?[as]\.?\b', lower):
        return "MA"
    return "Other"


