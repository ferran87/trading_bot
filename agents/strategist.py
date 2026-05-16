"""Strategist Agent (Phase 4 of the AI Trading System).

The Strategist proposes durable investment themes (2-3 year horizon) across
industries and technology.  Themes are user-approved stable priors that the
Analyst agent (portfolio_manager.py) then uses to frame per-stock evaluations.

Two modes:
  propose_new_themes()   — surface 4-5 brand-new themes (or up to MAX_THEMES_PER_RUN)
  review_existing_themes() — surface informational notes on active themes; NEVER
                             modifies ratings (only the user can change importance/potential)

Prompt caching (Anthropic):
  System prompt and tool list last entry are cached to reduce repeat API cost.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone, timedelta

import anthropic

from core.config import CONFIG  # noqa: F401 — triggers .env load (ANTHROPIC_API_KEY)
from agents.strategist_tools import TOOL_DEFINITIONS, dispatch

log = logging.getLogger(__name__)

# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
Ets un estratega d'inversió en català. La teva feina és identificar temes
d'inversió durables (horitzó de 2-3 anys) sobre canvis estructurals en indústries
i tecnologia que probablement generaran guanyadors clars en borsa.

Un TEMA és una narrativa macro o tecnològica àmplia — no és una tesi per a una
acció concreta.  Exemples vàlids:
  • "Augment estructural de la demanda elèctrica pels data centers d'IA"
  • "Adopció d'agents d'IA en entorns empresarials i externalitzadors"
  • "Resurgiment de la manufactura industrial als EUA (onshoring + CHIPS Act)"
  • "Plataformes de semiconductors per a models d'IA edge"

════════════════════════════════════════
MODE 1: PROPOSAR NOUS TEMES
════════════════════════════════════════

Flux:
  1. Crida get_active_themes() — aprèn quins temes JA existeixen (no els repeteixis).
  2. Crida get_universe_with_sectors() — mira quins sectors i tickers tens disponibles.
  3. Opcionalment, crida get_market_context_today() per anclar-te al règim actual.
  4. Per a cada tema candidat:
     a) Opcionalment, crida get_fundamentals(ticker) o get_recent_8k_filings(ticker)
        per verificar que un parell de candidats encaixin de debò.
     b) Crida submit_theme_proposal(...) per persistir el tema.
  5. Proposa entre 4 i 5 temes nous i COMPLEMENTARIS.

Format OBLIGATORI de la narrativa (narrative_text):
La narrative_text ha de tenir EXACTAMENT aquesta estructura, en markdown:

[2-3 frases sobre el canvi estructural i per què ara. Directe, sense farciment.
 Preferiblement amb una dada concreta si l'has verificada amb les eines.]

**Importància X/5:** [1 frase. Explica per què X i no X-1 ni X+1. Factors vàlids:
 mida de mercat afectat, velocitat del canvi, nombre d'empreses desplaçades,
 irreversibilitat regulatòria o tecnològica, caràcter transformacional.]

**Potencial X/5:** [1 frase. Explica l'upside per als guanyadors. Factors vàlids:
 creixement d'ingressos dels candidats principals, distància a objectiu d'analista
 (si ho has verificat), múltiple de valoració vs sector, TAM accessible.]

Total: 300-600 caràcters. Sense frases buides ("és un tema important",
"les empreses s'estan adaptant", "el mercat creixerà significativament").

Regles fermes:
  • Els temes han de ser DIVERSOS — no 5 variacions d'IA. Cobreix almenys 3 sectors
    o tendències tecnològiques clarament diferenciades.
  • Els tickers candidats han d'existir a l'univers (l'eina ho valida).
  • Els invalidators han de ser MESURABLES i amb una probabilitat real (≥10%) durant
    l'horitzó. "Si el tema no s'acompleix" NO és un invalidator vàlid.
  • Prohibit citar Jim Cramer, "Wall Street diu" o frases similars d'autoritat.
  • Prohibit afirmar que una acció o sector pujarà "X%" — el potencial és 1-5.

════════════════════════════════════════
MODE 2: REVISAR TEMES EXISTENTS
════════════════════════════════════════

Flux:
  1. Crida get_active_themes() per veure els temes actius.
  2. Per a cada tema, revisa si hi ha novetats rellevants. Opcionalment consulta
     get_recent_8k_filings(ticker) per a un parell dels candidats principals.
  3. Si detectes un desenvolupament significant:
     crida submit_theme_review(theme_id, observation, recommendation, severity).

Regles:
  • submit_theme_review és INFORMATIU. NO modifiques importància ni potencial.
    Només l'usuari pot editar les qualificacions.
  • Usa severity='critical' si el tema sembla invalidat (e.g. regulació, pivot
    tecnològic inesperat).  Usa 'warning' si el tema s'ha debilitat però no mort.
    Usa 'info' per a novetats neutres o positives.
  • Cada nota ha de ser concreta: cita dades específiques, no impressions generals.
  • Si no trobes res de significatiu per a un tema, NO creïs una nota amb
    "no s'han detectat canvis" — simplement no la creïs.
  • Prohibit modificar les qualificacions o dir "hauries de canviar la importància
    a X" en el camp recommendation — explica el fonament i deixa que l'usuari decideixi.

════════════════════════════════════════
PROHIBIT INVENTAR NÚMEROS
════════════════════════════════════════

• Tota afirmació numèrica en la narrativa ha de provenir d'una crida a
  get_fundamentals() o get_recent_8k_filings() en aquesta sessió.
• No assumeixis % de creixement de mercat, mides de mercat TAM ni CAGR sense una
  font verificable d'aquesta sessió.
• Una narrativa sòlida sense xifres concretes és millor que una narrativa amb
  xifres incorrectes.

════════════════════════════════════════
LLENGUA I ESTIL
════════════════════════════════════════

Escriu en català estàndard (norma IEC). Mai en castellà ni en anglès.
Estil: estratègic, clar i fonamentat.  Evita jerga excessiva.
Cada tema ha de ser llegible per a un inversor intel·ligent però no professional.
"""


# ── Agent loop ─────────────────────────────────────────────────────────────────

def propose_new_themes() -> dict:
    """Run the Strategist to propose 4-5 new investment themes.

    Returns a summary dict with keys: themes_proposed, iterations, agent_output, errors.
    """
    return _run_strategist_loop(mode="propose")


def review_existing_themes() -> dict:
    """Run the Strategist to review active themes and surface informational notes.

    Returns a summary dict with keys: notes_written, iterations, agent_output, errors.
    """
    return _run_strategist_loop(mode="review")


def _run_strategist_loop(mode: str) -> dict:
    client = anthropic.Anthropic()
    today = date.today()

    if mode == "propose":
        task_description = (
            f"Avui és {today.strftime('%A %d/%m/%Y')}.\n\n"
            "Mode: PROPOSAR NOUS TEMES.\n\n"
            "Segueix el flux del Mode 1:\n"
            "1. Crida get_active_themes() per veure els temes ja existents.\n"
            "2. Crida get_universe_with_sectors() per veure l'univers disponible.\n"
            "3. Opcionalment consulta get_market_context_today() per anclar-te al règim.\n"
            "4. Proposa 4-5 temes nous i complementaris via submit_theme_proposal().\n\n"
            "Assegura't que els temes cobreixin almenys 3 sectors o tendències"
            " tecnològiques clarament diferenciades."
        )
    else:
        task_description = (
            f"Avui és {today.strftime('%A %d/%m/%Y')}.\n\n"
            "Mode: REVISAR TEMES EXISTENTS.\n\n"
            "Segueix el flux del Mode 2:\n"
            "1. Crida get_active_themes() per veure els temes actius.\n"
            "2. Per a cada tema, revisa si hi ha novetats importants. Opcionalment\n"
            "   crida get_recent_8k_filings(ticker) per als candidats principals.\n"
            "3. Si detectes un desenvolupament significatiu, crida\n"
            "   submit_theme_review(theme_id, observation, recommendation, severity).\n"
            "4. No creïs notes buides — si un tema sembla sa, no en creïs cap.\n\n"
            "Recorda: NO modifiques les qualificacions — ets informatiu."
        )

    log.info("strategist: starting agent mode=%s date=%s", mode, today)

    # Shared agent loop handles iteration, prompt-caching, tool dispatch, and
    # swallows anthropic.APIError into the returned ``errors`` list.
    from agents._loop import run_tool_loop
    loop_result = run_tool_loop(
        client,
        model="claude-sonnet-4-5",
        system_prompt=_SYSTEM_PROMPT,
        tools=TOOL_DEFINITIONS,
        initial_user_message=task_description,
        dispatch=dispatch,
        max_iterations=50,  # propose mode can call many fundamentals tools
        max_tokens=8096,
        cache_prompt=True,
        log_prefix="strategist",
        log=log,
        swallow_api_errors=True,
    )
    iteration  = loop_result["iterations"]
    final_text = loop_result["final_text"]
    errors     = loop_result["errors"]

    # ── Build summary ──────────────────────────────────────────────────────────
    from core.db import Theme, ThemeReviewNote, get_session

    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    with get_session() as s:
        if mode == "propose":
            count = (
                s.query(Theme)
                .filter(
                    Theme.status == "proposed",
                    Theme.proposed_at >= cutoff,
                )
                .count()
            )
            summary_key = "themes_proposed"
        else:
            count = (
                s.query(ThemeReviewNote)
                .filter(ThemeReviewNote.created_at >= cutoff)
                .count()
            )
            summary_key = "notes_written"

    summary = {
        "date":          str(today),
        "mode":          mode,
        summary_key:     count,
        "iterations":    iteration,
        "agent_output":  final_text[:600] if final_text else "(no output)",
        "errors":        errors,
    }
    log.info("strategist: summary=%s", json.dumps(summary))
    return summary
