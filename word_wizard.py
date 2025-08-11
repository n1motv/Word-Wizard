#!/usr/bin/env python3
"""
Word Wizard v0.1 – générateur de wordlists (pentest éthique)
© 2025 – GPL-3.0   |   Créé par n1motv 😎

- “Enrichisseur doux” : synonymes/lemmes (WordNet), translitérations (unidecode),
  variantes & diminutifs (assets/variants.json + fallback interne).
- Checkbox “Ajouter variantes”.
- Mode bancaire : utilise aussi les dates pour l’alphabet de chiffres.
- Capitalisation par token (Hello_World, Jean-Pierre → Jean-Pierre).
- Exports Hashcat/John : .hcmask, .rule, basewords + rules.
"""
from __future__ import annotations
import sys, itertools, math, gzip, pathlib, json, datetime as dt, re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup  # (laisse pour compat future/OSINT)
import requests                # (laisse pour compat future/OSINT)

# Dépendances optionnelles pour l’enrichisseur
try:
    from unidecode import unidecode  # translitération (accents → ASCII)
except Exception:
    def unidecode(s: str) -> str:  # fallback neutre
        return s

try:
    # NLTK WordNet (synonymes + lemmatisation légère via morphy)
    from nltk.corpus import wordnet as wn
    _WN_OK = True
    # Si les data ne sont pas dispo, l'appel lèvera une exception plus tard ; on gère.
except Exception:
    wn = None
    _WN_OK = False

from PyQt6.QtCore    import Qt, QThread, pyqtSignal
from PyQt6.QtGui     import QFont, QIcon, QPalette, QColor, QBrush, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QListWidget, QListWidgetItem,
    QStackedWidget, QFileDialog, QMessageBox, QVBoxLayout, QHBoxLayout,
    QFormLayout, QLineEdit, QPushButton, QSpinBox, QCheckBox,
    QLabel, QProgressBar, QTextEdit, QFrame, QComboBox
)

# ─────────── I18N ───────────
LANGS = {"fr":"Français","en":"English","ar":"العربية","ru":"Русский","zh":"中文"}
def load_tr(code: str) -> dict[str,str]:
    try:
        with open(f"assets/lang/{code}.json","r",encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}

# ─────────── Variantes prénoms/diminutifs ───────────
DEFAULT_VARIANTS: dict[str,list[str]] = {
    # (fallback minimal — chargez assets/variants.json pour une couverture large)
    "alex": ["alexis","alexandre","aleks","alec","sasha","lex","aleksei","alexy"],
    "mohamed": ["mohammad","muhammad","mohammed","mehdi","hamid","hamoud"],
    "jean": ["jean-pierre","jean-marc","jeannot","j.p.","jp"],
    "william": ["will","bill","billy","liam","willie"],
    "elizabeth": ["liz","lizzy","beth","betty","liza","elle"],
    "katherine": ["kate","katie","kat","cathy","kathryn","kathy"],
    "robert": ["rob","bob","bobby","robbie","bert","roberto"],
    "michael": ["mike","miky","mickael","michel","micka"],
    "andrew": ["andy","drew","andré","andrei"],
    "nicolas": ["nico","nikos","nik","nikola","nicholas"],
}

def load_name_variants() -> dict[str,list[str]]:
    """Charge assets/variants.json si présent; sinon renvoie DEFAULT_VARIANTS."""
    path = pathlib.Path("assets/variants.json")
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # normalise en {lower: list[str lower]}
            norm = {}
            for k, vals in data.items():
                k2 = str(k).strip().lower()
                v2 = sorted({str(x).strip().lower() for x in (vals or []) if str(x).strip()})
                if k2:
                    norm[k2] = list(v2)
            return norm
        except Exception:
            pass
    # fallback
    return {k.lower(): [v.lower() for v in vs] for k,vs in DEFAULT_VARIANTS.items()}

# ─────────── Modèle ───────────
SEP_SET, VOWELS = [" ","_","-",".","/","!","@","#","$","%","?",","], "aeiouyAEIOUY"

@dataclass
class Options:
    min_len:int=6
    max_len:int=20
    separators:list[str]=field(default_factory=lambda:SEP_SET)
    add_leet:bool=True
    add_casing:bool=True
    add_separators:bool=True
    cap_first:   bool = False  # capitaliser chaque token (Hello_World)
    cap_last:    bool = False  # capitaliser la dernière lettre
    add_prefix_suffix:str="!@#$%?"
    duplicate_vowels:bool=False
    reverse_words:bool=False
    numeric_only:bool=False         # pour mode bancaire
    exact_digits:int=0              # nombre de chiffres requis
    add_variants:bool=False         # ✅ Enrichisseur doux

    def validate(self):
        if self.min_len > self.max_len:
            raise ValueError("min_len>max_len")
        if not(1 <= self.min_len <= 128 and 1 <= self.max_len <= 128):
            raise ValueError("length out of range")
        if self.numeric_only and self.exact_digits < 1:
            raise ValueError("Exact digits must be >=1 in banking mode")

class WordlistEngine:
    LEET = str.maketrans("aAeEiIoOsS","@4€3!1¡0$5")

    def __init__(self, words:list[str], dates:list[dt.date], opt:Options, variants_map:dict[str,list[str]]|None=None):
        self.words = [w for w in words if w]
        self.dates = dates
        self.o     = opt; self.o.validate()
        self.variants_map = variants_map or {}
        # construit une table inversée simple pour retrouver “fratrie” de variantes
        self._variant_siblings: dict[str,set[str]] = {}
        for base, alts in self.variants_map.items():
            allv = {base.lower(), *[a.lower() for a in alts]}
            for v in allv:
                self._variant_siblings.setdefault(v, set()).update(allv - {v})

    # ---- API principale ----
    def generate(self)->list[str]:
        if self.o.numeric_only:
            return self._generate_numeric()

        # enrichissement doux sur les mots utilisateur (pas sur les dates)
        base_words = self._enrich_words(self.words) if self.o.add_variants else list(self.words)

        parts   = self._dates() + base_words
        combos  = self._combine(parts)
        combos  = self._mutate(combos)
        combos  = self._prefix_suffix(combos)
        combos  = self._filter(combos)
        return self._uniq(combos)

    # ---- Mode bancaire (chiffres uniquement) ----
    def _generate_numeric(self) -> list[str]:
        length = self.o.exact_digits
        nums = []
        # récupérer les chiffres depuis les mots ET les dates
        src = "".join(self.words) + "".join(self._dates())
        # alphabet de chiffres dédupliqué pour éviter les doublons inutiles
        digits = "".join(sorted(set(re.findall(r"\d", src))))
        if not digits:
            raise ValueError("Aucun chiffre dans les informations fournies (mots/dates)")
        for combo in itertools.product(digits, repeat=length):
            nums.append("".join(combo))
        return self._uniq(nums)

    # ---- Enrichisseur doux ----
    def _enrich_words(self, words:list[str]) -> list[str]:
        pool: set[str] = set()
        # sécurité : borne pour éviter explosions si champs longs
        MAX_SYNONYMS_PER_WORD = 8

        for w in words:
            if not w: 
                continue
            pool.add(w)

            # variantes sans accents / translitération
            w_unacc = unidecode(w)
            if w_unacc and w_unacc != w:
                pool.add(w_unacc)

            # découpe en tokens simples (pour prénoms composés, etc.)
            tokens = [t for t in re.split(r"[\s_\-]+", w) if t]

            # variantes prénoms/diminutifs (sur token)
            for t in tokens:
                t0 = t.lower()
                # all siblings depuis table inversée
                sibs = self._variant_siblings.get(t0, set())
                for s in sibs:
                    # garde la casse du token original pour l’esthétique minimale
                    pool.add(self._reapply_case(s, t))

            # WordNet : lemmes + synonymes
            if _WN_OK and wn is not None:
                try:
                    # lemma simple
                    m = wn.morphy(w.lower())
                    if m:
                        pool.add(self._reapply_case(m, w))

                    # synonymes (noms, adj, verbes…)
                    syns: set[str] = set()
                    for syn in wn.synsets(w.lower()):
                        for l in syn.lemma_names():
                            cand = l.replace("_"," ")
                            if cand and cand.isascii():  # simple filtre
                                syns.add(cand)
                            if len(syns) >= MAX_SYNONYMS_PER_WORD:
                                break
                        if len(syns) >= MAX_SYNONYMS_PER_WORD:
                            break
                    for s in syns:
                        pool.add(self._reapply_case(s, w))
                except Exception:
                    # si wordnet n’est pas dispo/chargé → ignorer silencieusement
                    pass

        # petit nettoyage
        cleaned = {x.strip() for x in pool if x and x.strip()}
        return list(cleaned)

    @staticmethod
    def _reapply_case(src: str, ref: str) -> str:
        """Réapplique grossièrement la casse du mot de référence.
        - REF tout en maj → retourne upper
        - REF tout en min → retourne lower
        - Sinon → capitalise premier token (esthétique douce)
        """
        if ref.isupper():
            return src.upper()
        if ref.islower():
            return src.lower()
        # capitalize premier token
        parts = re.split(r'(\W+)', src)
        for i,p in enumerate(parts):
            if p and p[0].isalpha():
                parts[i] = p[0].upper() + p[1:].lower()
                break
        return "".join(parts)

    # ---- Dates formatées ----
    def _dates(self):
        out=[]; fmt=lambda d,f:d.strftime(f)
        for d in self.dates:
            dd,mm,yy,yyyy = fmt(d,"%d"),fmt(d,"%m"),fmt(d,"%y"),fmt(d,"%Y")
            out += [dd,mm,yy,yyyy, dd+mm, mm+dd, dd+mm+yy, dd+mm+yyyy, yyyy+mm+dd]
        return out

    # ---- Combinaisons ----
    def _combine(self, items):
        out=[]
        for r in range(1, min(4,len(items))+1):
            for prod in itertools.permutations(items, r):
                if self.o.add_separators:
                    for s in self.o.separators: out.append(s.join(prod))
                else:
                    out.append("".join(prod))
        return out

    # ---- Mutations ----
    def _mutate(self, lst):
        acc = set(lst)
        for w in list(acc):
            if self.o.add_casing:
                acc.update({w.upper(), w.lower(), w.capitalize()})
            if self.o.add_leet:
                acc.add(w.translate(self.LEET))
            if self.o.duplicate_vowels:
                acc.add(re.sub(f"([{VOWELS}])", r"\\1\\1", w))
            if self.o.reverse_words:
                acc.add(w[::-1])
            # capitaliser chaque token
            if self.o.cap_first and w:
                acc.add(self._cap_first_tokens(w))
            if self.o.cap_last and len(w) > 0:
                acc.add(w[:-1].lower() + w[-1].upper())
        return list(acc)

    @staticmethod
    def _cap_first_tokens(s: str) -> str:
        # on coupe sur les "non-alphanum" mais on garde les séparateurs
        parts = re.split(r'(\W+)', s, flags=re.UNICODE)
        for i, p in enumerate(parts):
            if p and p[0].isalpha():
                parts[i] = p[0].upper() + p[1:].lower()
        return "".join(parts)

    # ---- Pré/Suffixes ----
    def _prefix_suffix(self, lst):
        if not self.o.add_prefix_suffix:
            return lst
        ret=[]; extra=self.o.add_prefix_suffix
        for w in lst:
            for ch in extra:
                ret += [ch+w, w+ch]
        return lst+ret

    # ---- Filtres & utilitaires ----
    def _filter(self, lst):
        return [w for w in lst if self.o.min_len<=len(w)<=self.o.max_len]

    @staticmethod
    def _uniq(lst):
        seen, out = set(), []
        for w in lst:
            if w not in seen:
                seen.add(w); out.append(w)
        return out

    @staticmethod
    def entropy(w):
        return len(w)*math.log2(len(set(w)) or 1)

    # ---- Export helpers ----
    def basewords(self)->list[str]:
        """‘Mots de base’ : dates formatées + champs saisis (+ enrichis si activé), sans combinaisons."""
        words = self._enrich_words(self.words) if self.o.add_variants else list(self.words)
        return self._uniq(self._dates() + words)

# ─────────── Thread ───────────
class GenThread(QThread):
    done = pyqtSignal(list)
    def __init__(self, eng):
        super().__init__()
        self.eng=eng
    def run(self):
        try:
            res = self.eng.generate()
        except Exception:
            res = []  # silence
        self.done.emit(res)

# ─────────── GUI ───────────
class Wizard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setObjectName("MainWindow")
        self.lang   = "fr"; self.tr = load_tr(self.lang)
        self.setWindowTitle("Word Wizard")
        self.resize(984, 774)
        self.setWindowIcon(QIcon("assets/logo.png"))

        self._set_modern_palette()
        QApplication.instance().setFont(QFont("Segoe UI",11))

        # Charger les variantes de prénoms/diminutifs
        self.name_variants = load_name_variants()

        # Top bar
        bar = QFrame(); bar.setObjectName("TopBar")
        logo = QLabel(); pix=QPixmap("assets/logo.png")
        if not pix.isNull():
            logo.setPixmap(pix.scaledToHeight(100,Qt.TransformationMode.SmoothTransformation))
        else:
            logo.setText("Word Wizard")
        logo.setObjectName("Logo")
        self.lang_box = QComboBox()
        for code,name in LANGS.items():
            self.lang_box.addItem(name,code)
        self.lang_box.currentIndexChanged.connect(self._change_lang)
        top_layout = QHBoxLayout(bar)
        top_layout.addWidget(logo)
        top_layout.addStretch()
        top_layout.addWidget(self.lang_box)

        # Sidebar navigation
        self.nav = QListWidget()
        for name in ("tab_sources","tab_options","tab_preview"):
            item = QListWidgetItem(self._t(name))
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.nav.addItem(item)
        self.nav.setCurrentRow(0)
        self.nav.currentRowChanged.connect(self._on_nav_changed)
        self.nav.setObjectName("SideNav")

        # Pages container
        self.stack = QStackedWidget()
        self._init_pages()
        self.generated: list[tuple[str,str,float]] = []
        self.engine: WordlistEngine | None = None

        # Main layout
        central = QWidget()
        main_layout = QVBoxLayout(central)
        main_layout.addWidget(bar)
        content = QHBoxLayout()
        content.addWidget(self.nav)
        content.addWidget(self.stack,1)
        main_layout.addLayout(content)

        # footer
        self.foot = QLabel(self._t("created_by"))
        self.foot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.foot.setStyleSheet("""
            background: qlineargradient(
                x1:0, y1:0, x2:1, y2:0,
                stop:0 #00ffff, stop:1 #ff00ff
            );
            color: #1c2124;
            padding: 8px;
            font-size: 20pt;
            font-weight: bold;
            border-radius: 8px;
        """)
        main_layout.addWidget(self.foot)

        self.setCentralWidget(central)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setBrush(
            QPalette.ColorRole.Window,
            QBrush(QPixmap("assets/background.png").scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation
            ))
        )
        self.setPalette(pal)

        # style global
        self.setStyleSheet("""
        /* Top bar */
        #TopBar { background: #282A2E; }
        #Logo { color: #00FFFF; font-size: 18pt; font-weight: bold; }

        /* Sidebar */
        #SideNav {
            background: qlineargradient(x1:0,y1:0,x2:0,y2:1,
                stop:0 #202124, stop:1 #282A2E);
            color: #E8EAED; border-right:2px solid #00FFFF;
        }
        #SideNav::item { padding:12px; }
        #SideNav::item:selected {
            background:#00FFFF;color:#202124;border-radius:4px;
        }

        /* Widgets communs */
        QLineEdit, QTextEdit, QSpinBox, QComboBox {
            background:#202124;color:#E8EAED;border:2px solid #00FFFF;
            border-radius:6px;padding:6px;
        }
        QListWidget {
            background:#FFFFFF;color:#000000;
            border:2px solid #FF00FF;border-radius:6px;
        }
        QPushButton { border:none;padding:8px;border-radius:4px;
            font-weight:bold;
        }
        QPushButton#generateButton {
            background:#00FFFF;color:#202124;
        }
        QPushButton#exportButton {
            background:#FF00FF;color:#202124;
        }
        QPushButton:hover { opacity:0.9; }

        /* Pages */
        .Page { background:#282A2E;border-radius:8px;padding:12px; }
                           
        /* Style de la progress bar */
        QProgressBar {
            background: #303134;
            border: 2px solid #00FFFF;
            border-radius: 6px;
            text-align: center;
        }
        QProgressBar::chunk {
            background: qlineargradient(
                x1:0, y1:0, x2:1, y2:0,
                stop:0 #00ffff, stop:1 #ff00ff
            );
            border-radius: 4px;
        }
        """)

    def _set_modern_palette(self):
        pal = QPalette()
        pal.setColor(QPalette.ColorRole.Window, QColor("#202124"))
        pal.setColor(QPalette.ColorRole.Base,   QColor("#282A2E"))
        pal.setColor(QPalette.ColorRole.Text,   QColor("#E8EAED"))
        pal.setColor(QPalette.ColorRole.Button, QColor("#303134"))
        pal.setColor(QPalette.ColorRole.ButtonText, QColor("#E8EAED"))
        pal.setColor(QPalette.ColorRole.Highlight, QColor("#1A73E8"))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
        app = QApplication.instance(); app.setPalette(pal); app.setStyle("Fusion")

    def _init_pages(self):
        # ─── Sources ─────────────────────────
        src = QWidget(); src.setObjectName("Page")
        form = QFormLayout(src); form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.fields = {}
        for key,label in [
            ("first","First name"),("last","Last name"),
            ("nick","Nickname"),("city","City"),
            ("company","Company"),("interest","Interest"),
            ("github","GitHub username"),("linkedin","LinkedIn username"),
            ("instagram","Instagram username"),("facebook","Facebook username"),
        ]:
            le = QLineEdit(); le.setToolTip(label)
            le.setPlaceholderText(self._t(key))
            form.addRow(QLabel(self._t(key)+" :"), le)
            self.fields[key]=le
        self.date_edit = QLineEdit(); self.date_edit.setToolTip("Dates (CSV)")
        self.date_edit.setPlaceholderText(self._t("dates_placeholder"))
        form.addRow(QLabel(self._t("dates_placeholder")+" :"), self.date_edit)
        self.extra_words = QTextEdit()
        self.extra_words.setPlaceholderText(self._t("extra_words_title"))
        form.addRow(QLabel(self._t("extra_words_title")+" :"), self.extra_words)
        self.stack.addWidget(src)

        # ─── Options ─────────────────────────
        opt = QWidget(); opt.setObjectName("Page")
        f2 = QFormLayout(opt); f2.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # combo "Type de compte"
        self.account_type = QComboBox()
        self.account_type.addItem(self._t("account_web"), None)
        services = [
            "Facebook","Instagram","LinkedIn","GitHub","Snapchat",
            "Tinder","Netflix","Hulu","Twitter","Reddit","Pinterest",
            "YouTube","TikTok","WhatsApp","Telegram","Slack",
            "Discord","Steam","Xbox Live","PlayStation Network",
            "Amazon","eBay","PayPal","Stripe","Google",
            "Microsoft","Apple","Dropbox","Salesforce","Instagram Business",
            "Spotify","SoundCloud","Zoom","Skype","Twitch",
            "Vimeo","WeChat","QQ","Line","KakaoTalk",
            "Uber","Airbnb","Lyft","Grab","Booking.com",
            "Deliveroo","DoorDash","Bank Account"
        ]
        for svc in services:
            self.account_type.addItem(svc, svc)
        self.account_type.currentIndexChanged.connect(self._on_account_type_changed)
        f2.addRow(QLabel(self._t("account_web")+" :"), self.account_type)

        # spinbox pour mode bancaire (nombre de chiffres)
        self.bank_digits = QSpinBox(); self.bank_digits.setRange(4, 16)
        self.bank_digits.setValue(10)
        self.bank_digits.setEnabled(False)
        f2.addRow(QLabel(self._t("bank_digits")+" :"), self.bank_digits)

        self.min_len = QSpinBox(); self.min_len.setRange(1,128); self.min_len.setValue(6)
        self.max_len = QSpinBox(); self.max_len.setRange(1,128); self.max_len.setValue(10)
        self.sep_field = QLineEdit("".join(SEP_SET))
        self.sep_field.setPlaceholderText(self._t("separators"))
        self.chk_leet = QCheckBox(self._t("leet"))
        self.chk_case = QCheckBox(self._t("case"))
        self.chk_sep  = QCheckBox(self._t("use_separators"))
        self.chk_dup  = QCheckBox(self._t("dup_vowels"))
        self.chk_rev  = QCheckBox(self._t("reverse"))
        self.chk_cap_first = QCheckBox(self._t("cap_first"))
        self.chk_cap_last  = QCheckBox(self._t("cap_last"))
        # ✅ Nouveau : Enrichisseur doux
        self.chk_variants = QCheckBox(self._t("add_variants") or "Ajouter variantes (synonymes/translit/diminutifs)")

        self.extra_chars = QLineEdit("!@#$%")
        self.extra_chars.setPlaceholderText(self._t("prefix_suffix"))

        f2.addRow(self._t("min_length")+" :", self.min_len)
        f2.addRow(self._t("max_length")+" :", self.max_len)
        f2.addRow(self._t("separators")+" :", self.sep_field)
        for cb in (self.chk_leet,self.chk_case,self.chk_sep,self.chk_dup,self.chk_rev,self.chk_cap_first,self.chk_cap_last,self.chk_variants):
            f2.addRow(cb)
        f2.addRow(self._t("prefix_suffix")+" :", self.extra_chars)

        self.stack.addWidget(opt)

        # ─── Preview ─────────────────────────
        prev = QWidget(); prev.setObjectName("Page")
        v = QVBoxLayout(prev)
        self.lbl_filter = QLabel(self._t("filter")+" :")
        filt = QHBoxLayout(); filt.addWidget(self.lbl_filter)
        self.cb_weak   = QCheckBox(self._t("weak"))
        self.cb_fair   = QCheckBox(self._t("fair"))
        self.cb_strong = QCheckBox(self._t("strong"))
        for cb in (self.cb_weak,self.cb_fair,self.cb_strong):
            cb.setChecked(True)
            cb.stateChanged.connect(self._display_filtered)
            filt.addWidget(cb)
        filt.addStretch()
        self.lbl_legend = QLabel(
            f'<span style="color:#DD2222">■ {self._t("weak")}</span>  '
            f'<span style="color:#EEEE00">■ {self._t("fair")}</span>  '
            f'<span style="color:#22DD22">■ {self._t("strong")}</span>'
        )
        filt.addWidget(self.lbl_legend)
        v.addLayout(filt)

        btns = QHBoxLayout()
        self.btn_gen = QPushButton(self._t("generate")); self.btn_gen.setObjectName("generateButton")
        self.btn_exp = QPushButton(self._t("export"));   self.btn_exp.setObjectName("exportButton")
        self.btn_exp.setEnabled(False)
        self.btn_copy = QPushButton(self._t("copy")); self.btn_copy.setObjectName("exportButton")
        btns.addWidget(self.btn_copy); self.btn_copy.setEnabled(False)
        self.btn_copy.clicked.connect(self._copy_to_clipboard)

        # ---- Exports pro ----
        self.rule_style = QComboBox()
        self.rule_style.addItems(["Hashcat","John"])
        self.rule_style.setToolTip(self._t("rule_style") or "Style de règles")

        self.btn_exp_masks = QPushButton(self._t("export_masks") or "Export masks (.hcmask)")
        self.btn_exp_masks.setObjectName("exportButton")
        self.btn_exp_rule  = QPushButton(self._t("export_rule")  or "Export .rule")
        self.btn_exp_rule.setObjectName("exportButton")
        self.btn_exp_bwr   = QPushButton(self._t("export_bwr")   or "Export basewords + rules")
        self.btn_exp_bwr.setObjectName("exportButton")

        self.btn_exp_masks.clicked.connect(self._export_masks)
        self.btn_exp_rule.clicked.connect(self._export_rule_file)
        self.btn_exp_bwr.clicked.connect(self._export_basewords_and_rules)

        btns.addWidget(self.btn_gen)
        btns.addWidget(self.btn_exp)
        btns.addWidget(self.rule_style)
        btns.addWidget(self.btn_exp_rule)
        btns.addWidget(self.btn_exp_masks)
        btns.addWidget(self.btn_exp_bwr)

        v.addLayout(btns)

        # ─── Barre de recherche ───────────────────
        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText(self._t("search_placeholder"))
        self.search_bar.textChanged.connect(self._filter_search)
        v.addWidget(self.search_bar)

        self.list      = QListWidget()
        self.bar       = QProgressBar(); self.bar.setVisible(False)
        self.info      = QLabel()
        self.questions = QTextEdit(); self.questions.setReadOnly(True)
        v.addWidget(self.list); v.addWidget(self.bar); v.addWidget(self.info)
        self.lbl_suggested = QLabel(self._t("suggested_questions")+" :")
        v.addWidget(self.lbl_suggested); v.addWidget(self.questions)

        self.stack.addWidget(prev)

        # connexions
        self.btn_gen.clicked.connect(self._start)
        self.btn_exp.clicked.connect(self._export)

    # ---------- Helpers Export Pro ----------
    @staticmethod
    def _char_to_mask(ch:str)->str:
        if 'a' <= ch <= 'z': return "?l"
        if 'A' <= ch <= 'Z': return "?u"
        if '0' <= ch <= '9': return "?d"
        # hashcat ?s = specials ASCII. Pour tout le reste, on tombe sur ?a
        specials = r""" !\"#$%&'()*+,-./:;<=>?@[\]^_`{|}~"""
        return "?s" if ch in specials else "?a"

    def _current_words(self)->list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def _build_hashcat_rules(self)->list[str]:
        o = self.engine.o if self.engine else Options()
        rules = set()
        rules.add(":")  # no-op
        if o.cap_first:
            rules.add("c")                   # capitalize
        if o.cap_last:
            for n in range(0, max(1, o.max_len)):
                rules.add("lT%d" % n)       # lower then toggle pos n
        if o.add_leet:
            for a,b in (("a","@"),("a","4"),("e","3"),("i","1"),("o","0"),("s","5")):
                rules.add(f"s{a}{b}"); rules.add(f"s{a.upper()}{b}")
        for ch in (o.add_prefix_suffix or ""):
            rules.add("$"+ch)
        return [r for r in sorted(rules)]

    def _build_john_rules(self)->list[str]:
        o = self.engine.o if self.engine else Options()
        rules = set()
        rules.add("")  # no-op JtR
        if o.cap_first:
            rules.add("c")
        if o.add_leet:
            for a,b in (("a","@"),("a","4"),("e","3"),("i","1"),("o","0"),("s","5")):
                rules.add(f"s{a}{b}"); rules.add(f"s{a.upper()}{b}")
        for ch in (o.add_prefix_suffix or ""):
            rules.add("A"+ch)  # append
        return [r for r in sorted(rules)]

    # files
    def _export_masks(self):
        words = self._current_words()
        if not words:
            QMessageBox.warning(self, "Info", "Aucune donnée à exporter (liste vide).")
            return
        masks = {}
        for w in words:
            m = "".join(self._char_to_mask(c) for c in w)
            masks[m] = masks.get(m,0)+1
        ordered = sorted(masks.items(), key=lambda kv: (-kv[1], kv[0]))
        path,_ = QFileDialog.getSaveFileName(
            self, self._t("export_masks") or "Export masks",
            "masks.hcmask",
            "Hashcat masks (*.hcmask);;All files (*)"
        )
        if not path: return
        try:
            with open(path,"w",encoding="utf-8") as fh:
                for m,_cnt in ordered:
                    fh.write(m+"\n")
            QMessageBox.information(self, self._t("export_ok"), path)
        except Exception as e:
            QMessageBox.critical(self, self._t("error"), str(e))

    def _export_rule_file(self):
        style = self.rule_style.currentText()
        rules = self._build_hashcat_rules() if style=="Hashcat" else self._build_john_rules()
        if not rules:
            QMessageBox.warning(self, "Info", "Aucune règle à écrire.")
            return
        default = "hashcat.rule" if style=="Hashcat" else "john.rule"
        filt    = "Rule files (*.rule);;All files (*)"
        path,_ = QFileDialog.getSaveFileName(self, self._t("export_rule") or "Export .rule", default, filt)
        if not path: return
        try:
            with open(path,"w",encoding="utf-8") as fh:
                header = [
                    f"# Generated by Word Wizard — {style} rules",
                    "# Options activées :",
                    f"#  - cap_first={self.engine.o.cap_first if self.engine else False}",
                    f"#  - cap_last={self.engine.o.cap_last  if self.engine else False}",
                    f"#  - leet={self.engine.o.add_leet     if self.engine else False}",
                    f"#  - suffixes='{self.engine.o.add_prefix_suffix if self.engine else ''}'",
                    f"#  - add_variants={self.engine.o.add_variants if self.engine else False}",
                    ""
                ]
                fh.write("\n".join(header))
                for r in rules:
                    fh.write((r or ":") + "\n")
            QMessageBox.information(self, self._t("export_ok"), path)
        except Exception as e:
            QMessageBox.critical(self, self._t("error"), str(e))

    def _export_basewords_and_rules(self):
        if not self.engine:
            QMessageBox.warning(self,"Info","Génère d’abord une liste (bouton Générer).")
            return
        folder = QFileDialog.getExistingDirectory(self, self._t("export_bwr") or "Export basewords + rules")
        if not folder: return
        style = self.rule_style.currentText()
        rules = self._build_hashcat_rules() if style=="Hashcat" else self._build_john_rules()
        base  = self.engine.basewords()
        try:
            base_path  = pathlib.Path(folder) / "basewords.txt"
            rules_path = pathlib.Path(folder) / ("hashcat.rule" if style=="Hashcat" else "john.rule")
            base_path.write_text("\n".join(base), encoding="utf-8")
            with open(rules_path,"w",encoding="utf-8") as fh:
                fh.write("\n".join(r or ":" for r in rules))
            QMessageBox.information(
                self, self._t("export_ok"),
                f"{base_path}\n{rules_path}"
            )
        except Exception as e:
            QMessageBox.critical(self, self._t("error"), str(e))

    # ---------- UI actions ----------
    def _copy_to_clipboard(self):
        pwds = [self.list.item(i).text() for i in range(self.list.count())]
        if not pwds:
            return
        full_text = "\n".join(pwds)
        QApplication.clipboard().setText(full_text)
        QMessageBox.information(self, self._t("copied"), self._t("copied_to_clipboard"))

    def _filter_search(self, text: str):
        text = text.lower()
        self.list.clear()
        for w,cat,ent in self.generated:
            if text in w.lower():
                if cat=="weak"   and not self.cb_weak.isChecked():   continue
                if cat=="fair"   and not self.cb_fair.isChecked():   continue
                if cat=="strong" and not self.cb_strong.isChecked(): continue
                item = QListWidgetItem(w)
                col  = {"weak":"#8B0000","fair":"#B8860B","strong":"#006400"}[cat]
                item.setForeground(QBrush(QColor(col)))
                self.list.addItem(item)

    def _on_account_type_changed(self, idx:int):
        svc = self.account_type.currentData()
        mapping = {
            None:                    (6,10,True,True,False,False),
            "Bank Account":          (0,0,False,False,False,True),
            "Facebook":              (6,12,True,True,True,False),
            "Instagram":             (6,30,True,True,True,False),
            "LinkedIn":              (3,100,False,True,False,False),
            "GitHub":                (1,39,False,True,False,False),
            "Snapchat":              (3,15,False,False,False,False),
            "Tinder":                (6,100,True,True,True,False),
            "Netflix":               (4,60,True,True,True,False),
            "Hulu":                  (4,60,True,True,True,False),
            "Twitter":               (6,15,False,True,True,False),
            "Reddit":                (3,20,False,True,True,False),
            "Pinterest":             (3,15,False,True,True,False),
            "YouTube":               (3,50,False,True,True,False),
            "TikTok":                (2,24,False,True,True,False),
            "WhatsApp":              (10,13,False,False,False,False),
            "Telegram":              (5,32,False,True,False,False),
            "Slack":                 (1,80,False,True,False,False),
            "Discord":               (2,32,False,True,False,False),
            "Steam":                 (3,32,False,True,False,False),
            "Xbox Live":             (3,16,False,True,False,False),
            "PlayStation Network":   (5,16,False,True,False,False),
            "Amazon":                (6,128,True,True,True,False),
            "eBay":                  (6,128,True,True,True,False),
            "PayPal":                (8,64,True,True,True,False),
            "Stripe":                (6,64,True,True,True,False),
            "Google":                (8,100,True,True,True,False),
            "Microsoft":             (8,100,True,True,True,False),
            "Apple":                 (8,100,True,True,True,False),
            "Dropbox":               (8,64,True,True,True,False),
            "Salesforce":            (8,64,True,True,True,False),
            "Instagram Business":    (5,30,True,True,True,False),
            "Spotify":               (8,20,True,True,True,False),
            "SoundCloud":            (6,20,True,True,True,False),
            "Zoom":                  (6,32,True,True,True,False),
            "Skype":                 (6,32,True,True,True,False),
            "Twitch":                (4,25,True,True,True,False),
            "Vimeo":                 (4,25,True,True,True,False),
            "WeChat":                (1,50,False,True,False,False),
            "QQ":                    (1,16,False,False,False,False),
            "Line":                  (1,50,False,True,False,False),
            "KakaoTalk":             (1,50,False,True,False,False),
            "Uber":                  (4,100,True,True,True,False),
            "Airbnb":                (4,50,True,True,True,False),
            "Lyft":                  (4,50,True,True,True,False),
            "Grab":                  (4,50,True,True,True,False),
            "Booking.com":           (8,64,True,True,True,False),
            "Deliveroo":             (4,50,True,True,True,False),
            "DoorDash":              (4,50,True,True,True,False),
        }
        mn,mx,leet,case_,sep,bank = mapping.get(svc, mapping[None])
        if bank:
            self.min_len.setEnabled(False)
            self.max_len.setEnabled(False)
            self.sep_field.setEnabled(False)
            self.chk_leet.setEnabled(False)
            self.chk_case.setEnabled(False)
            self.chk_sep.setEnabled(False)
            self.chk_dup.setEnabled(False)
            self.chk_rev.setEnabled(False)
            self.extra_chars.setEnabled(False)
            self.bank_digits.setEnabled(True)
            self.chk_variants.setEnabled(False)  # variantes inutiles en mode numérique pur
        else:
            self.min_len.setEnabled(True)
            self.max_len.setEnabled(True)
            self.sep_field.setEnabled(True)
            self.chk_leet.setEnabled(True)
            self.chk_case.setEnabled(True)
            self.chk_sep.setEnabled(True)
            self.chk_dup.setEnabled(True)
            self.chk_rev.setEnabled(True)
            self.extra_chars.setEnabled(True)
            self.bank_digits.setEnabled(False)
            self.chk_variants.setEnabled(True)
        if not bank:
            self.min_len.setValue(mn)
            self.max_len.setValue(mx)
            self.chk_leet.setChecked(leet)
            self.chk_case.setChecked(case_)
            self.chk_sep.setChecked(sep)

    def _on_nav_changed(self,index:int):
        self.stack.setCurrentIndex(index)

    def _start(self):
        try:
            opts = Options(
                min_len=self.min_len.value(),
                max_len=self.max_len.value(),
                separators=list(self.sep_field.text()),
                add_leet=self.chk_leet.isChecked(),
                add_casing=self.chk_case.isChecked(),
                add_separators=self.chk_sep.isChecked(),
                add_prefix_suffix=self.extra_chars.text(),
                duplicate_vowels=self.chk_dup.isChecked(),
                reverse_words=self.chk_rev.isChecked(),
                cap_first=self.chk_cap_first.isChecked(),
                cap_last=self.chk_cap_last.isChecked(),
                numeric_only=(self.account_type.currentText()=="Bank Account"),
                exact_digits=self.bank_digits.value(),
                add_variants=self.chk_variants.isChecked(),
            )
            keys = ["first","last","nick","city","company","interest",
                    "github","linkedin","instagram","facebook"]
            words = [self.fields[k].text() for k in keys]
            words += [ln.strip() for ln in self.extra_words.toPlainText().splitlines() if ln.strip()]
            dates = [self._parse_date(d) for d in self.date_edit.text().split(",") if d.strip()]
            self.engine = WordlistEngine(words, dates, opts, variants_map=self.name_variants)
        except Exception as e:
            QMessageBox.critical(self, self._t("error"), str(e))
            return

        self.btn_gen.setEnabled(False)
        self.bar.setVisible(True); self.bar.setRange(0,0)
        self.thread = GenThread(self.engine)
        self.thread.done.connect(self._show)
        self.thread.start()

    def _show(self, words:list[str]):
        self.generated=[]
        for w in words:
            ent = WordlistEngine.entropy(w)
            cat = "strong" if ent>=60 else "fair" if ent>=40 else "weak"
            self.generated.append((w,cat,ent))
        self._display_filtered()
        avg = sum(e for _,_,e in self.generated)/max(len(self.generated),1)
        self.info.setText(f"{len(self.generated)} pw – avg ent ≈ {avg:.1f} bits")
        self.questions.setPlainText(self._make_questions())
        self.btn_gen.setEnabled(True)
        self.btn_exp.setEnabled(True)
        self.bar.setVisible(False)
        self.btn_copy.setEnabled(True)

    def _display_filtered(self):
        self.list.clear()
        for w,cat,_ in self.generated:
            if cat=="weak"   and not self.cb_weak.isChecked(): continue
            if cat=="fair"   and not self.cb_fair.isChecked(): continue
            if cat=="strong" and not self.cb_strong.isChecked(): continue
            item = QListWidgetItem(w)
            col  = {"weak":"#8B0000","fair":"#B8860B","strong":"#006400"}[cat]
            item.setForeground(QBrush(QColor(col)))
            self.list.addItem(item)

    def _export(self):
        path,_ = QFileDialog.getSaveFileName(self, self._t("export"),
            "wordlist.txt.gz", "Lists (*.txt *.gz)")
        if not path: return
        data = "\n".join(self.list.item(i).text() for i in range(self.list.count()))
        p = pathlib.Path(path)
        try:
            if p.suffix == ".gz":
                gzip.open(p,"wt",encoding="utf-8").write(data)
            else:
                p.write_text(data,encoding="utf-8")
            QMessageBox.information(self, self._t("export_ok"), str(p))
        except Exception as e:
            QMessageBox.critical(self, self._t("error"), str(e))

    def _make_questions(self)->str:
        qs=[]; f=self.fields
        if f["first"].text():    qs.append(self._t("q_first_name"))
        if f["last"].text():     qs.append(self._t("q_last_name"))
        if f["nick"].text():     qs.append(self._t("q_nickname"))
        if f["city"].text():     qs.append(self._t("q_city"))
        if f["company"].text():  qs.append(self._t("q_company"))
        if f["interest"].text():
            qs.append(self._t("q_interest").format(f['interest'].text()))
        dob=self.date_edit.text().strip()
        if dob:
            qs.extend([self._t("q_dob_day"),
                       self._t("q_dob_month"),
                       self._t("q_dob_year")])
        if f["github"].text():    qs.append(self._t("q_github"))
        if f["linkedin"].text():  qs.append(self._t("q_linkedin"))
        if f["instagram"].text(): qs.append(self._t("q_instagram"))
        if f["facebook"].text():  qs.append(self._t("q_facebook"))
        for extra in self.extra_words.toPlainText().splitlines():
            if extra.strip():
                qs.append(self._t("q_extra").format(extra.strip()))
        qs.extend(self._generic_questions())
        return "\n".join(qs)

    def _generic_questions(self) -> list[str]:
        return [
            "What was the name of your first pet?",
            "What was the make of your first car?",
            "What was the name of your elementary school?",
            "What is your favorite movie?",
            "What is your mother's maiden name?",
            "What is the name of the street you grew up on?",
            "Who was your childhood hero?",
            "What was the name of your first vacation destination?",
            "What is your preferred holiday activity?",
            "What was the name of your first teacher?",
            "Who was your favorite childhood friend?",
            "What was the brand of your first mobile phone?",
            "What was the name of your first boss?",
            "What is your favorite book?",
            "Where did you meet your spouse or partner?",
            "What was the name of your first roommate?",
            "What is your favorite sports team?",
            "What was your childhood nickname?",
            "What is your favorite restaurant?",
            "In which hospital were you born?",
            "What is your favorite city?",
            "What was your first job title?",
            "What was the model of your first computer?",
            "What is your favorite band?",
            "Who was your favorite high-school teacher?",
            "What was the name of the beach you last visited?",
            "What is your favorite vacation spot?",
            "What was your childhood house number?",
            "What was the name of your first girlfriend or boyfriend?",
            "What is your favorite TV show?",
            "What was the brand of your first bicycle?",
            "What was your high-school mascot?",
            "What company did you intern for first?",
            "What was your favorite subject in school?",
            "What is your favorite dessert?",
            "What color was your childhood bedroom?",
            "What breed was your childhood pet?",
            "What is your favorite game?",
            "What was the street name of your first home?",
            "What was the title of your first published article?",
            "Who was your first sports coach?",
            "What was your favorite ice-cream flavor?",
            "What was the name of the first concert you attended?",
            "What is your favorite holiday?",
            "What did you want to be when you were a child?",
            "What is the license-plate number of your first car?",
            "What is your favorite smartphone app?",
            "What is your favorite quote?",
            "What was the name of your first school-bus driver?",
            "What was the first website you ever visited?",
            "What was your childhood phone number?",
            "What is your father's middle name?",
            "What was your dream job as a child?",
            "What was the name of your first boat?",
            "What was your favorite candy as a child?",
            "Where did you go on your first airplane ride?",
            "What was your father's occupation?",
            "What was your grandmother’s first name?",
            "What was your grandfather’s first name?",
            "What was your favorite toy as a child?",
            "What was the name of your first babysitter?",
            "What was the name of your first summer camp?",
            "What was your first e-mail address username?",
            "What was the name of your first landlord?",
            "What was the name of your first pet’s veterinarian?",
            "What is your favorite ice-cream truck’s flavor?",
            "What was your favorite cartoon character?",
            "What was the name of your first mentor?",
            "What was the make of your first bicycle?",
            "What was your childhood best friend’s name?",
            "What was the destination of your first airline flight?",
            "What was the name of the first restaurant you dined at?",
            "What was your first bank’s name?",
            "What was your favorite sport in high school?",
            "What was your first gaming console?",
            "What was the name of your first music teacher?",
            "What was your first pet's nickname?",
            "What street was your childhood home on?",
            "What was the name of your first art teacher?",
            "What was the name of your first veterinarian?",
            "What was your childhood favorite book?",
            "Who was your favorite author as a child?",
            "What was the title of the first movie you saw?",
            "Where did you go on your first school trip?",
            "What was your favorite subject in middle school?",
            "What was the color of your first bicycle?",
            "What was the model of your first watch?",
            "What was the name of your first sports team?",
            "What was your first car’s registration number?",
            "What is your favorite board game?",
            "What was the name of your first garden plant?",
            "What was the name of your first piano teacher?",
            "What was the first book you ever read?",
            "What was your first cellphone’s phone number?",
            "What was your first roommate’s nickname?",
            "What was the name of your first neighborhood?",
            "What was your childhood dream vacation?",
            "What was the name of your first ice-cream truck?"
        ]

    def _change_lang(self,_):
        self.lang=self.lang_box.currentData()
        self.tr=load_tr(self.lang)
        self._translate_ui()

    def _translate_ui(self):
        t = self._t
        # sidebar
        for i, key in enumerate(("tab_sources","tab_options","tab_preview")):
            self.nav.item(i).setText(t(key))
        # placeholders & labels pour Sources
        for k,le in self.fields.items():
            le.setPlaceholderText(t(k))
            lbl = le.parentWidget().layout().labelForField(le)
            if lbl: lbl.setText(t(k)+" :")
        self.date_edit.setPlaceholderText(t("dates_placeholder"))
        self.extra_words.setPlaceholderText(t("extra_words_title"))
        # Options
        self.account_type.setItemText(0, t("account_web"))

        lbl = self.account_type.parentWidget().layout().labelForField(self.account_type)
        if lbl: lbl.setText(t("account_web") + " :")

        lbl = self.bank_digits.parentWidget().layout().labelForField(self.bank_digits)
        if lbl: lbl.setText(t("bank_digits") + " :")

        lbl = self.min_len.parentWidget().layout().labelForField(self.min_len)
        if lbl: lbl.setText(t("min_length") + " :")
        lbl = self.max_len.parentWidget().layout().labelForField(self.max_len)
        if lbl: lbl.setText(t("max_length") + " :")

        lbl = self.sep_field.parentWidget().layout().labelForField(self.sep_field)
        if lbl: lbl.setText(t("separators") + " :")
        self.sep_field.setPlaceholderText(t("separators"))

        lbl = self.extra_chars.parentWidget().layout().labelForField(self.extra_chars)
        if lbl: lbl.setText(t("prefix_suffix") + " :")
        self.extra_chars.setPlaceholderText(t("prefix_suffix"))
        self.chk_cap_first.setText(t("cap_first"))
        self.chk_cap_last .setText(t("cap_last"))

        self.chk_leet.setText(t("leet"))
        self.chk_case.setText(t("case"))
        self.chk_sep .setText(t("use_separators"))
        self.chk_dup .setText(t("dup_vowels"))
        self.chk_rev .setText(t("reverse"))
        self.chk_variants.setText(t("add_variants") or "Ajouter variantes (synonymes/translit/diminutifs)")

        # Preview
        self.lbl_filter.setText(t("filter")+" :")
        self.cb_weak.setText(t("weak"))
        self.cb_fair.setText(t("fair"))
        self.cb_strong.setText(t("strong"))
        self.lbl_legend.setText(
            f'<span style="color:#DD2222">■ {t("weak")}</span>  '
            f'<span style="color:#EEEE00">■ {t("fair")}</span>  '
            f'<span style="color:#22DD22">■ {t("strong")}</span>'
        )
        self.btn_gen.setText(t("generate"))
        self.btn_exp.setText(t("export"))

        # nouveaux textes
        self.btn_copy.setText(t("copy"))
        self.search_bar.setPlaceholderText(t("search_placeholder"))
        self.rule_style.setToolTip(t("rule_style") or "Style de règles")
        self.btn_exp_rule.setText(t("export_rule") or "Export .rule")
        self.btn_exp_masks.setText(t("export_masks") or "Export masks (.hcmask)")
        self.btn_exp_bwr.setText(t("export_bwr") or "Export basewords + rules")

        self.lbl_suggested.setText(t("suggested_questions")+" :")
        self.foot.setText(t("created_by"))

    def _t(self,k:str)->str:
        return self.tr.get(k,k)

    @staticmethod
    def _parse_date(txt:str)->dt.date:
        for fmt in ("%d/%m/%Y","%d-%m-%Y","%d/%m/%y","%d-%m-%y","%Y-%m-%d"):
            try:
                return dt.datetime.strptime(txt.strip(),fmt).date()
            except:
                pass
        raise ValueError(f"Bad date: {txt}")

def main():
    app=QApplication(sys.argv)
    w=Wizard(); w.show()
    sys.exit(app.exec())

if __name__=="__main__":
    main()
