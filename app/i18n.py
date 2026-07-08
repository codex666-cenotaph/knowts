"""Minimal translation layer for the profile language preference.

No i18n framework (matches PLAN.md's "no build step" philosophy) — just a flat
dict mapping each English source string to its Dutch translation, looked up
via ``t(key, lang)``. English source strings double as translation keys, so a
missing/未-added key harmlessly falls back to English instead of erroring.

Templates use ``{{ t('Some label') }}``; ``templating.render`` binds ``t`` to
the current user's ``language`` preference (default English) so callers never
pass the language explicitly.
"""

from __future__ import annotations

SUPPORTED_LANGUAGES: dict[str, str] = {"en": "English", "nl": "Nederlands"}
DEFAULT_LANGUAGE = "en"

# Names used in the "respond only in <language>" instruction injected into
# note-generation prompts (app/notes.py) — not the UI dict below.
LLM_LANGUAGE_NAMES: dict[str, str] = {"nl": "Dutch (Nederlands)"}

_NL: dict[str, str] = {
    # Nav / chrome
    "Upload": "Uploaden",
    "Meetings": "Vergaderingen",
    "Prompts": "Prompts",
    "Users": "Gebruikers",
    "Profile": "Profiel",
    "Sign out": "Uitloggen",
    "admin": "beheerder",
    "knowts v{version} · meeting MP3 → notes": "knowts v{version} · vergadering-mp3 → notities",
    "← Back to home": "← Terug naar start",
    # Home / upload
    "New meeting": "Nieuwe vergadering",
    "Upload a meeting recording; knowts transcribes it and generates notes with the prompts you pick.":
        "Upload een opname van een vergadering; knowts transcribeert deze en genereert notities met de prompts die je kiest.",
    "Title": "Titel",
    "Meeting date": "Vergaderdatum",
    "Audio file": "Audiobestand",
    "Notes to generate": "Te genereren notities",
    "Auto-detect": "Automatisch detecteren",
    "Transcription and notes use this language. Auto-detect lets whisper guess.":
        "Transcriptie en notities gebruiken deze taal. Automatisch detecteren laat whisper raden.",
    "No prompts available.": "Geen prompts beschikbaar.",
    "Upload & process": "Uploaden & verwerken",
    "Recent meetings": "Recente vergaderingen",
    "All meetings →": "Alle vergaderingen →",
    "official": "officieel",
    # Meetings archive
    "Search by title": "Zoeken op titel",
    "From": "Van",
    "To": "Tot",
    "Filter": "Filteren",
    "Clear": "Wissen",
    "Clear filter": "Filter wissen",
    "No meetings match your filter.": "Geen vergaderingen komen overeen met je filter.",
    "No meetings yet.": "Nog geen vergaderingen.",
    "Upload one →": "Upload er een →",
    "Name": "Naam",
    "Date": "Datum",
    "Duration": "Duur",
    "Status": "Status",
    "Notes": "Notities",
    # Meeting detail
    "Delete": "Verwijderen",
    "Delete this meeting, its transcript and notes?": "Deze vergadering, het transcript en de notities verwijderen?",
    "Audio": "Audio",
    "Download original": "Origineel downloaden",
    "Transcript": "Transcript",
    "Download .txt": "Download .txt",
    "Download .srt": "Download .srt",
    "No notes generated yet.": "Nog geen notities gegenereerd.",
    "Copy": "Kopiëren",
    "Copied!": "Gekopieerd!",
    "Download .md": "Download .md",
    "Generate more notes": "Meer notities genereren",
    "Run": "Uitvoeren",
    "Refresh to see results ↻": "Ververs om resultaten te zien ↻",
    # Job / meeting status + job kind (shown as badges)
    "processing": "verwerken",
    "transcribing": "transcriberen",
    "generating": "genereren",
    "done": "klaar",
    "error": "fout",
    "queued": "in wachtrij",
    "running": "actief",
    "transcribe": "transcriptie",
    "notes": "notities",
    # Prompt manager
    "New prompt": "Nieuwe prompt",
    "Prompts turn a transcript into notes. Official prompts (🔒) are the shared library; clone any prompt to make an editable personal copy.":
        "Prompts zetten een transcript om in notities. Officiële prompts (🔒) vormen de gedeelde bibliotheek; kloon een prompt voor een bewerkbare persoonlijke kopie.",
    "Search name or description": "Zoek op naam of omschrijving",
    "Include archived": "Inclusief gearchiveerd",
    "Search": "Zoeken",
    "Kind": "Soort",
    "Description": "Omschrijving",
    "Versions": "Versies",
    "Actions": "Acties",
    "archived": "gearchiveerd",
    "personal": "persoonlijk",
    "Clone": "Klonen",
    "Restore": "Herstellen",
    "Archive": "Archiveren",
    "Archive this prompt? It stays out of the picker but past notes keep their reference.":
        "Deze prompt archiveren? Hij valt weg uit de kiezer, maar bestaande notities behouden hun verwijzing.",
    "No prompts match. ": "Geen prompts komen overeen. ",
    "Create one →": "Maak er een →",
    "← All prompts": "← Alle prompts",
    "read-only for you — clone it to make changes": "alleen-lezen voor jou — kloon hem om wijzigingen te maken",
    "System message": "Systeembericht",
    "Template": "Template",
    "must contain": "moet bevatten",
    "Reduce template": "Samenvoeg-template",
    "(optional; merges partial results for long transcripts)": "(optioneel; voegt deelresultaten samen bij lange transcripten)",
    "Model": "Model",
    "(optional; blank uses the default)": "(optioneel; leeg gebruikt de standaard)",
    "Temperature": "Temperatuur",
    "Max tokens": "Max. tokens",
    "Official prompt (shared, read-only for members)": "Officiële prompt (gedeeld, alleen-lezen voor leden)",
    "Create prompt": "Prompt aanmaken",
    "Save": "Opslaan",
    "Clone to edit": "Klonen om te bewerken",
    "Test run": "Testrun",
    "Preview this prompt against a transcript snippet before saving. Uses the fields above (long snippets are truncated).":
        "Bekijk een voorbeeld van deze prompt op een transcript-fragment voordat je opslaat. Gebruikt de velden hierboven (lange fragmenten worden afgekapt).",
    "Transcript snippet": "Transcript-fragment",
    "Paste a bit of a transcript...": "Plak een stukje transcript...",
    "Run test": "Test uitvoeren",
    "Version history": "Versiegeschiedenis",
    "Each edit stores a new version so past notes stay attributable.": "Elke bewerking bewaart een nieuwe versie zodat eerdere notities herleidbaar blijven.",
    "current": "huidig",
    "Preview": "Voorbeeld",
    # Profile
    "Signed in as": "Ingelogd als",
    "Change password": "Wachtwoord wijzigen",
    "Current password": "Huidig wachtwoord",
    "New password": "Nieuw wachtwoord",
    "Confirm new password": "Bevestig nieuw wachtwoord",
    "Update password": "Wachtwoord bijwerken",
    "Language": "Taal",
    "Interface language. Selecting Dutch also asks the notes generator to respond only in Dutch, regardless of the transcript's language.":
        "Interfacetaal. Bij Nederlands vraagt de notitiegenerator ook om alleen in het Nederlands te antwoorden, ongeacht de taal van het transcript.",
    "Save language": "Taal opslaan",
    # Admin users
    "Create user": "Gebruiker aanmaken",
    "Create": "Aanmaken",
    "username": "gebruikersnaam",
    "password": "wachtwoord",
    "All users": "Alle gebruikers",
    "Username": "Gebruikersnaam",
    "Role": "Rol",
    "Created": "Aangemaakt",
    "you": "jij",
    "active": "actief",
    "inactive": "inactief",
    "Deactivate": "Deactiveren",
    "Reactivate": "Heractiveren",
    "new password": "nieuw wachtwoord",
    "Reset": "Resetten",
    # Login
    "Sign in to convert meeting recordings into notes.": "Log in om vergaderopnames om te zetten in notities.",
    "Sign in": "Inloggen",
    "Password": "Wachtwoord",
}

_TRANSLATIONS: dict[str, dict[str, str]] = {"nl": _NL}


def t(key: str, lang: str) -> str:
    """Translate ``key`` (the English source string) into ``lang``.

    Falls back to the English source when ``lang`` is English/unknown or the
    key has no translation yet, so untranslated strings degrade gracefully
    instead of raising or rendering blank.
    """
    return _TRANSLATIONS.get(lang, {}).get(key, key)
