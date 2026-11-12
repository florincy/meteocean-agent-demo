"""Versioned pilot data and validation shared by the web service and desktop."""
from hashlib import sha256
import json
from pathlib import Path
import re


API = "/api/review/v1"
FIXTURES = Path(__file__).with_name("fixtures")
SPECS = (
    ("horario-1", "time", "Inicio", "Início · primeiro mergulho", (
        ("diver-1", "Mergulhador 1", "O8.00", "08:00"),
        ("diver-2", "Mergulhador 2", "08:OO", ""),
    )),
    ("horario-fim-1", "time", "Fim", "Fim · primeiro mergulho", (
        ("end-diver-1", "Mergulhador 1", "10.1O", "10:10"),
        ("end-diver-2", "Mergulhador 2", "1O:1O", ""),
    )),
    ("celula-profundidade", "cell", "Profundidade_Max_m", "Profundidade máxima (m)", (
        ("depth", "ROM de teste · mergulho 1", "1O,5", "10"),
    )),
    ("celula-duracao", "cell", "Tempo_Fundo_min", "Tempo de fundo (min)", (
        ("duration", "ROM de teste · mergulho 1", "3B", "33"),
    )),
)


def checksum(data):
    return sha256(data).hexdigest()


def canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def pilot_fixture():
    tasks, images = {}, {}
    for key, kind, field, title, slots in SPECS:
        cards = []
        for slot, label, raw, value in slots:
            data = (FIXTURES / f"{slot}.png").read_bytes()
            digest = checksum(data)
            images[digest] = data
            cards.append(dict(id=slot, label=label, raw=raw, value=value, crop=digest, required=True))
        task = dict(id=key, kind=kind, field=field, title=title, cards=cards)
        task["version"] = checksum(canonical(task).encode())
        tasks[key] = task
    return tasks, images


def validate_entries(task, entries):
    slots = {card["id"] for card in task["cards"]}
    if not entries or set(entries) - slots:
        raise ValueError("Selecione um recorte disponível para revisão.")
    clean = {}
    for slot, entry in entries.items():
        state = entry["state"]
        literal = entry["transcription"].strip()
        value = entry["operational_value"].strip()
        if state in {"EMPTY", "UNREADABLE"}:
            if literal or value:
                raise ValueError("Deixe os valores vazios ao marcar vazio ou ilegível.")
        elif state == "READABLE":
            if not literal or not value or any(ord(character) < 32 for character in literal):
                raise ValueError("Preencha a transcrição literal e o valor operacional.")
            if task["kind"] == "time":
                if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
                    raise ValueError("Use um horário válido em HH:MM.")
            elif task["field"] == "Profundidade_Max_m":
                if not re.fullmatch(r"[0-9]{1,6}(?:[.,][0-9]{1,3})?", value):
                    raise ValueError("Informe a profundidade em metros, com ponto ou vírgula decimal.")
                value = value.replace(",", ".")
            elif not re.fullmatch(r"[0-9]{1,6}", value):
                raise ValueError("Informe o tempo de fundo em minutos inteiros.")
        else:
            raise ValueError("Escolha um julgamento válido.")
        clean[slot] = dict(state=state, transcription=literal, operational_value=value)
    if len(clean) > 1 and (set(clean) != slots or
                          any(entry["state"] != "READABLE" for entry in clean.values()) or
                          len({entry["operational_value"] for entry in clean.values()}) != 1):
        raise ValueError("O julgamento em conjunto exige duas transcrições e um horário comum.")
    return clean
