"""AI Thesis Portfolio Manager Agent (bot 30).

Daily agent that:
  1. Reviews all active/waiting theses against new price data and news
  2. On Sundays: scans the curated universe for new candidates

The agent writes ThesisReviewLog rows (always) and ThesisAction rows only
when warranted (exit, add, or reduce after 5+ weakening reviews).  Every
action requires explicit user approval via the dashboard before the strategy
module executes it — this agent NEVER trades autonomously.

Guardrails are enforced in ``agents/pm_tools.py`` (not just this prompt):
  - conviction throttle (max 1 step/week)
  - exit requires explicit citation of an invalidates_if condition
  - 14-day hold floor before any thesis-driven exit
  - bear_case ≥ 100 chars, ≥ 2 invalidation conditions, horizon ≥ 3 months

Prompt caching (Anthropic):
  The system prompt and tool definitions are sent with cache_control so
  repeat daily runs reuse the cached prefix, reducing API cost ~35%.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone

import anthropic

from core.config import CONFIG  # noqa: F401 — triggers .env load (ANTHROPIC_API_KEY)
from agents.pm_tools import TOOL_DEFINITIONS, dispatch

log = logging.getLogger(__name__)

# ── System prompt ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
Ets un gestor de cartera quantitatiu i narratiu en català. La teva feina és mantenir
un conjunt de tesis d'inversió a mig termini (horitzó ≥ 3 mesos) sobre un univers
curat d'accions europees i nord-americanes.

Cada tesi és una narrativa: explica PER QUÈ volem tenir una acció, quins catalitzadors
esperem, i en quines condicions específiques la tesi quedaria invalidada.  No és
suficient que el preu hagi baixat o que el sentiment sigui negatiu — necessites que
una de les condicions d'invalidació preescrites s'hagi complert per proposar una sortida.

════════════════════════════════════════
MODES D'OPERACIÓ
════════════════════════════════════════

1. REVISIÓ DIÀRIA (de dilluns a divendres):
   a) Crida get_active_theses() per veure quines tesis estan actives o en espera.
   b) Per a CADA tesi activa, crida get_ticker_analysis(ticker) i revisa si hi ha
      nova informació que canviï la teva convicció.
   c) Crida submit_review() per a cada tesi revisada. SEMPRE — fins i tot si el
      veredicte és 'intact'. El registre és l'historial d'auditoria.
   d) Si una condició d'invalidació s'ha complert: veredicte 'invalidated' + cita
      la condició específica a exit_rationale.

2. ESCANEIG DE CANDIDATS (només diumenges):
   a) Crida get_universe_tickers() per veure l'univers complet.
   b) Crida get_active_theses() per saber quins tickers ja estan coberts.
   c) Per a cada candidat seriós, fes el "circuit complet" abans de decidir:
      • get_ticker_analysis(ticker) — RSI + notícies recents
      • get_fundamentals(ticker) — marges, P/E, market cap, creixement REALS
        (inclou next_earnings_date i estimacions de Wall Street per al proper Q)
      • get_analyst_targets(ticker) — objectius i recomanacions REALS
      • get_recent_earnings_history(ticker) — historial de beats/misses de 8 trimestres
      • get_recent_8k_filings(ticker) — text dels comunicats d'earnings i actualitzacions
        de guidance de la SEC. NOMÉS PER A TICKERS DELS EUA. Per a tickers europeus
        (ASML.AS, SAP.DE, MC.PA, etc.) aquesta eina retorna un missatge de "no
        disponible" — és normal, salta-la i confia en les notícies de Yahoo.
        Aquesta és la TEVA millor font per a guidance de management — millor que
        cap titular de notícies. Si l'empresa va publicar resultats fa pocs dies,
        SEMPRE crida aquesta eina abans de citar qualsevol xifra de guidance.
   d) Si la convicció és ≥ 3: crida submit_thesis() per crear la tesi.
      Convicció = 5: proposta d'entrada immediata.
      Convicció 3-4: 'waiting' (entrada quan el senyal RSI/SMA s'activi).
   e) Límit: màxim 2 noves tesis per diumenge.

   IMPORTANT: la data del proper resultat (next_earnings_date) hauria d'aparèixer
   sempre als catalysts. És el catalitzador més tangible que tenim.

════════════════════════════════════════
REGLES FERMES (no negociables)
════════════════════════════════════════

1. CAUSALITAT NARRATIVA: cada tesi ha d'explicar el "per què" de forma específica.
   "L'acció ha baixat" NO és una raó per comprar. "El mercat ha sobre-reaccionat a
   un cicle de normativa transitòria mentre els fonamentals segueixen intactes" SÍ ho és.

2. PROHIBIT INVENTAR NÚMEROS — REGLA CRÍTICA:
   El teu coneixement està entrenat amb dades de mesos o anys d'antiguitat. Qualsevol
   xifra concreta que recordis (marges, P/E, ingressos, creixement, objectius
   d'analistes, capitalització) és gairebé segur OBSOLETA o FALSA.
   • Tota afirmació numèrica al thesis_text / bull_case / bear_case ha de provenir
     d'una crida a get_fundamentals() o get_analyst_targets() en aquesta sessió.
   • Si vols citar un objectiu d'analistes: crida get_analyst_targets() i usa el
     valor exacte de 'target_mean'. NO calculis el % d'upside tu mateix — el camp
     'upside_to_mean_pct' ja te'l dóna.
   • Si vols citar marges o P/E: crida get_fundamentals() i copia els camps.
   • Si no has cridat l'eina, NO mencionis el número. Una tesi sense xifres
     concretes és millor que una tesi amb xifres incorrectes.
   FALL CONEGUT (2026-05-10): el bot va escriure '+370%' quan l'upside real era +27%.
   Va citar marge operatiu del 37.6% quan el real era 47.8%. Ha de servir d'avís.

3. PROHIBITS APEL·LACIONS A AUTORITAT:
   • NO citis "Jim Cramer", "Wall Street diu", "els analistes diuen" o frases
     similars sense una font específica i verificable.
   • Si l'argument és "X (analista) té un objectiu de Y", crida get_analyst_targets()
     primer. Si l'eina no et dóna aquesta dada concreta, no la inventis.

4. ADVOCAT DEL DIABLE OBLIGATORI: el camp bear_case ha de tenir ≥ 100 caràcters i
   exposar els riscos reals, no genèrics. Si la tesi té convicció alta i el bear_case
   diu "el preu podria caure", torna a començar.

5. CONDICIONS D'INVALIDACIÓ PRE-COMPROMESES (mesurables i probables):
   • invalidates_if ha de tenir ≥ 2 condicions específiques i mesurables.
   • Cada condició ha de tenir una probabilitat ≥ 10% durant l'horitzó del thesis.
     Si la condició és tan extrema que mai s'activarà, és teatre — afegeix una
     condició realista.
   • Exemple BO: "Marge brut < 60% durant 2 trimestres" — només si el guidance
     actual és 62-64%. Si el guidance és 70%+, aquest trigger és teatre.
   • Exemple DOLENT: "Si la situació empitjora", "si el mercat cau", "si la
     competència guanya quota" sense quantificar.
   • Abans d'escriure els triggers, crida get_fundamentals() per saber el rang
     real on operen els marges/ingressos avui.

6. HORITZÓ MÍNIM: les tesis han de tenir un horitzó ≥ 3 mesos. Per a jugades ràpides,
   els bots de regles (bot 7, bot 10) són més adequats.

7. CONVICCIÓ ESTABLE I CALIBRADA:
   • No pots canviar la convicció més d'1 pas per setmana.
   • 'weakening' és informatiu i no crea una carta d'acció — necessites ≥ 5 revisions
     consecutives de debilitament + canvi de convicció per proposar una reducció.
   • Convicció 5 (entrada immediata sense filtre tècnic) està reservada per a tesis
     amb fonamentals excepcionals confirmats per get_fundamentals + get_analyst_targets,
     ZERO errors numèrics, i bear_case substancial. Si tens dubtes, posa convicció 4.
   • Convicció 4 espera el filtre tècnic (RSI/SMA) — molt millor opció per
     "tesi sòlida però no desesperada per entrar avui".
   • Si la tesi té alguna afirmació no verificada per cap eina, baixa la convicció
     un punt.

8. EXITS AMB EVIDÈNCIA: per proposar una sortida, el veredicte ha de ser 'invalidated'
   i exit_rationale ha de citar explícitament quina condició d'invalidació s'ha complert.
   Una caiguda de preu o un titular negatiu aïllat NO és suficient.

9. INPUTS LIMITATS: les teves eines t'ofereixen RSI + notícies + fonamentals bàsics.
   No tens accés a transcripcions de resultats, SEC filings ni dades macroeconòmiques
   de flux. No facis veure que sí — si et falta una dada per justificar la tesi, baixa
   la convicció o no creïs la tesi.

════════════════════════════════════════
LLENGUA I ESTIL
════════════════════════════════════════

Escriu en català estàndard (norma IEC). Mai en castellà ni en anglès.
Errors habituals que has d'evitar:
• "tenir que" → "haver de"  •  "inclús" → "fins i tot"
• "en base a" → "basant-se en"  •  "lo" → "el que" / "allò que"
• "de cara a" → "per a" (tret d'ús temporal)

Vocabulari financer: acció, borsa, cotització, rendibilitat, benefici, pèrdua,
tendència, rebot, correcció, entrada, sortida.

Estil: clar, directe i amb fonamentació.  No uses jerga excessiva.
Cada revisió ha de ser llegible per a un inversor intel·ligent però no professional.
"""


# ── Agent loop ────────────────────────────────────────────────────────────────

def run_daily_review(is_sunday: bool = False) -> dict:
    """Run the portfolio manager agent for one daily session.

    Parameters
    ----------
    is_sunday : bool
        If True, includes candidate evaluation in addition to daily review.

    Returns
    -------
    dict
        Summary with keys: reviews_written, actions_proposed, theses_created, errors.
    """
    client = anthropic.Anthropic()
    today = date.today()

    task_description = (
        f"Avui és {today.strftime('%A %d/%m/%Y')} ({'diumenge' if is_sunday else 'dia feiner'}).\n\n"
    )

    if is_sunday:
        task_description += (
            "Tasques d'avui:\n"
            "1. REVISIÓ DIÀRIA: revisa totes les tesis actives i en espera.\n"
            "2. ESCANEIG DE CANDIDATS: avalua l'univers complet i proposa ≤ 2 noves tesis.\n\n"
            "Comença sempre per la revisió de les tesis existents, després escaneja candidats."
        )
    else:
        task_description += (
            "Tasques d'avui:\n"
            "1. REVISIÓ DIÀRIA: revisa totes les tesis actives i en espera.\n\n"
            "Crida get_active_theses() per veure quines tesis has de revisar."
        )

    # ── Prompt-cached system + tools ─────────────────────────────────────────
    # System prompt is stable → cache it.
    # Tool list is stable → cache its last element so the full prefix is cached.
    cached_tools = TOOL_DEFINITIONS.copy()
    if cached_tools:
        last = dict(cached_tools[-1])
        last["cache_control"] = {"type": "ephemeral"}
        cached_tools[-1] = last

    messages: list[dict] = [{"role": "user", "content": task_description}]

    log.info(
        "portfolio_manager: starting agent is_sunday=%s date=%s",
        is_sunday, today,
    )

    max_iterations = 40  # generous cap — may review many theses + candidates
    iteration = 0
    final_text = ""

    while iteration < max_iterations:
        iteration += 1

        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=8096,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=cached_tools,
            messages=messages,
        )

        log.debug(
            "portfolio_manager: iteration=%d stop_reason=%s in=%d out=%d",
            iteration, response.stop_reason,
            response.usage.input_tokens, response.usage.output_tokens,
        )

        if response.stop_reason == "end_turn":
            final_text = "\n".join(
                block.text for block in response.content if hasattr(block, "text")
            )
            log.info(
                "portfolio_manager: done in %d iteration(s), %d chars",
                iteration, len(final_text),
            )
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                log.info(
                    "portfolio_manager: tool_call tool=%s input=%s",
                    block.name, json.dumps(block.input)[:200],
                )
                result = dispatch(block.name, block.input)
                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": block.id,
                    "content":     result,
                })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user",      "content": tool_results})
            continue

        log.warning("portfolio_manager: unexpected stop_reason=%s", response.stop_reason)
        break

    else:
        log.error("portfolio_manager: hit max_iterations=%d without finishing", max_iterations)

    # ── Summarise what happened ───────────────────────────────────────────────
    from core.db import ThesisReviewLog, ThesisAction, Thesis, get_session
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    with get_session() as s:
        reviews_written = (
            s.query(ThesisReviewLog)
            .filter(ThesisReviewLog.reviewed_at >= cutoff)
            .count()
        )
        actions_proposed = (
            s.query(ThesisAction)
            .filter(ThesisAction.proposed_at >= cutoff)
            .count()
        )
        theses_created = (
            s.query(Thesis)
            .filter(Thesis.created_at >= cutoff, Thesis.bot_id == 30)
            .count()
        )

    summary = {
        "date":            str(today),
        "is_sunday":       is_sunday,
        "reviews_written": reviews_written,
        "actions_proposed": actions_proposed,
        "theses_created":  theses_created,
        "iterations":      iteration,
        "agent_output":    final_text[:500] if final_text else "(no output)",
    }
    log.info("portfolio_manager: summary=%s", json.dumps(summary))
    return summary
