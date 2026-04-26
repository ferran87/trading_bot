"""README / Summary tab — explains the project to Antonio and Adria."""
from __future__ import annotations

import streamlit as st


# ── Sector labels ──────────────────────────────────────────────────────────────

_US_SECTORS: dict[str, list[str]] = {
    "💻 Tecnologia / Creixement": ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA"],
    "🏦 Finances":                ["JPM", "V", "BAC", "GS"],
    "🏥 Salut":                   ["UNH", "JNJ", "PFE"],
    "⛽ Energia":                  ["XOM", "CVX"],
    "🏗 Industrials":              ["CAT", "HON"],
    "🛒 Consum bàsic":            ["PG", "KO", "WMT"],
    "🛍 Consum discrecional":     ["HD", "NKE", "DIS"],
}

_EU_SECTORS: dict[str, list[str]] = {
    "💻 Tecnologia":              ["ASML.AS", "SAP.DE"],
    "✈️ Aeroespacial / Luxe":    ["AIR.PA", "MC.PA"],
    "⚙️ Industrials":            ["SIE.DE"],
    "🏦 Finances":                ["BNP.PA", "ALV.DE"],
    "🏥 Salut / Farmàcia":       ["NOVN.SW", "BAYN.DE"],
    "🚗 Automòbil":              ["BMW.DE"],
    "🛒 Consum bàsic":           ["NESN.SW"],
    "⛽ Energia":                 ["TTE.PA"],
}

_ETFS: list[tuple[str, str]] = [
    ("SXR8.DE", "iShares Core S&P 500 UCITS — referència de mercat"),
    ("SXRV.DE", "iShares Nasdaq 100 UCITS"),
    ("ZPRR.DE", "SPDR Russell 2000 UCITS — petites empreses EUA"),
    ("EXSA.DE", "iShares Euro Stoxx 600 UCITS — renda variable europea àmplia"),
    ("XDWD.DE", "Xtrackers MSCI World UCITS — diversificació global"),
    ("QDVE.DE", "iShares S&P 500 IT Sector UCITS — tecnologia EUA"),
    ("QDVH.DE", "iShares S&P 500 Financials UCITS — finances EUA"),
]


def _badge(text: str, color: str) -> str:
    """Return an HTML badge span."""
    return (
        f"<span style='background:{color};color:white;padding:2px 10px;"
        f"border-radius:12px;font-size:0.82rem;font-weight:600'>{text}</span>"
    )


def render_readme_tab() -> None:
    """Render the full README / Summary tab."""

    # ── Header ─────────────────────────────────────────────────────────────────
    st.markdown("""
## 📖 Guia del Projecte

Benvinguts, **Antonio** i **Adria**. Aquesta pestanya explica com funciona el
sistema de trading automàtic: quin és el broker, quines accions es poden operar,
com identifiquem el tipus de mercat i com funciona cada bot.
""")

    st.divider()

    # ── Infrastructure ─────────────────────────────────────────────────────────
    st.markdown("### 🏦 Infraestructura")

    col1, col2, col3 = st.columns(3)
    with col1:
        with st.container(border=True):
            st.markdown("**Broker**")
            st.markdown("**Interactive Brokers (IBKR)**")
            st.caption(
                "Un dels brokers amb les comissions més baixes del món. "
                "Accés directe a borses europees i americanes."
            )
    with col2:
        with st.container(border=True):
            st.markdown("**Compte**")
            st.markdown("**Paper Trading — $1.000.000**")
            st.caption(
                "Diners virtuals per validar les estratègies sense risc real. "
                "Quan estiguem satisfets, podem passar al compte de diners reals."
            )
    with col3:
        with st.container(border=True):
            st.markdown("**Execució**")
            st.markdown("**Automàtica · Diària**")
            st.caption(
                "El bot s'executa cada dia laborable abans de l'obertura del mercat. "
                "Les ordres es col·loquen automàticament via IBKR Gateway."
            )

    st.markdown("""
**Com funciona el pressupost virtual?**
El compte IBKR és compartit, però cada bot té el seu propi *pressupost virtual*
de **$500.000** (50% del total cadascun). Cada bot gestiona el seu propi efectiu
i les seves pròpies posicions, de manera que el rendiment de cada estratègia és
mesurable per separat.
""")

    st.divider()

    # ── Universe ───────────────────────────────────────────────────────────────
    st.markdown("### 🗺 Univers d'Accions")
    st.caption(
        "El sistema analitza diàriament totes les accions i ETFs de la llista. "
        "Compra únicament les que compleixen les condicions de la seva estratègia."
    )

    tab_us, tab_eu, tab_etf = st.tabs(["🇺🇸 Accions EUA", "🇪🇺 Accions EU", "📦 ETFs UCITS"])

    with tab_us:
        st.caption(
            "Accions americanes d'alta liquiditat. "
            "Operades en USD però reportades en EUR al dashboard."
        )
        for sector, tickers in _US_SECTORS.items():
            cols = st.columns([2, 5])
            cols[0].markdown(f"**{sector}**")
            cols[1].markdown("  ".join(f"`{t}`" for t in tickers))

    with tab_eu:
        st.caption(
            "Accions europees en diverses borses (Xetra, Euronext, SIX Swiss). "
            "La majoria en EUR; NESN.SW i NOVN.SW en CHF."
        )
        for sector, tickers in _EU_SECTORS.items():
            cols = st.columns([2, 5])
            cols[0].markdown(f"**{sector}**")
            cols[1].markdown("  ".join(f"`{t}`" for t in tickers))

    with tab_etf:
        st.caption(
            "ETFs UCITS cotitzats a Xetra — aptes per a inversors retail europeus. "
            "`SXR8.DE` (S&P 500) s'usa també com a referència del mercat global."
        )
        for ticker, desc in _ETFS:
            cols = st.columns([2, 5])
            cols[0].markdown(f"`{ticker}`")
            cols[1].caption(desc)

    st.divider()

    # ── Market Regimes ─────────────────────────────────────────────────────────
    st.markdown("### 🌡 Règims de Mercat")
    st.markdown(
        "Cada dia classifiquem el mercat en un dels quatre règims basant-nos en "
        "**`SXR8.DE`** (el nostre proxy del S&P 500). Aquesta classificació "
        "determina quin bot és més eficient en cada moment."
    )

    r1, r2, r3, r4 = st.columns(4)

    with r1:
        with st.container(border=True):
            st.markdown("### 🟢 BULL")
            st.markdown("**Mercat alcista**")
            st.caption("RSI > 50 · Preu sobre SMA200 · Drawdown < 5%")
            st.markdown(
                "El mercat puja de manera sostinguda. "
                "El Trend Momentum funciona molt bé aquí."
            )

    with r2:
        with st.container(border=True):
            st.markdown("### 🟡 CORRECCIÓ")
            st.markdown("**Baixada moderada**")
            st.caption("RSI < 50 o Drawdown 5–15%")
            st.markdown(
                "Pullback dins una tendència alcista. "
                "El Trend Momentum captura el rebot. "
                "El RSI Compounder roman en cash."
            )

    with r3:
        with st.container(border=True):
            st.markdown("### ⬛ BEAR")
            st.markdown("**Mercat baixista**")
            st.caption("Preu sota SMA200 · Drawdown > 15%")
            st.markdown(
                "Tendència a la baixa prolongada. "
                "Ambdós bots tendeixen a romandre en efectiu. "
                "La protecció del capital és prioritat."
            )

    with r4:
        with st.container(border=True):
            st.markdown("### 🔴 CRASH")
            st.markdown("**Col·lapse ràpid**")
            st.caption("RSI < 30 o Drawdown > 20%")
            st.markdown(
                "Caiguda brusca del mercat. "
                "El RSI Compounder s'activa i busca accions que han caigut molt "
                "i comencen a recuperar-se."
            )

    st.divider()

    # ── Bot 7: RSI Compounder ──────────────────────────────────────────────────
    st.markdown("### 🤖 Bot 7 — RSI Compounder")

    left, right = st.columns([3, 2])
    with left:
        st.markdown("""
**Filosofia:** *Compra la por, deixa córrer els guanyadors.*

Aquest bot espera moments de pànic en el mercat — quan una acció ha caigut tant
que la majoria de la gent ven per por. Aleshores entra i espera la recuperació.
No és un bot actiu: pot estar mesos en efectiu fins que es dóna el moment adequat.
""")

        with st.expander("📋 Condicions d'entrada (totes han de complir-se)"):
            st.markdown("""
1. **Mercat global en crash:** `SXR8.DE` ha tingut RSI < 30 en els darrers 15 dies.
2. **Acció en capitulació:** el RSI de l'acció ha caigut per sota de **25** en els
   darrers 15 dies (senyal de sobrevenda extrema).
3. **Recuperació iniciada:** el RSI actual de l'acció és entre **40 i 65** — ja ha
   rebotut però no s'ha recuperat del tot.
4. **No massa calenta:** RSI actual < 65 per evitar entrar en accions ja
   molt recuperades.
""")

        with st.expander("🚪 Condicions de sortida (per ordre de prioritat)"):
            st.markdown("""
1. **Stop catastròfic:** si l'acció cau un **40%** des del cost mitjà → sortida immediata.
2. **Piràmide (acumula en caiguda):**
   - Si baixa un **8%** des del cost → compra un lot extra (redueix cost mitjà).
   - Si baixa un **15%** → compra un segon lot extra (màxim 3 lots per acció).
3. **Stop seguidor progressiu** (s'ajusta amb el RSI):
   - RSI < 70 → stop al **35%** des del màxim.
   - RSI entre 70–80 → stop al **20%** (mercat calent, protegim guanys).
   - RSI > 80 → stop al **12%** (eufòria, bloquem guanys al màxim).
4. **Sortida temporal:** màxim **90 dies** en posició, si mai ha estat en guanys.
""")

    with right:
        with st.container(border=True):
            st.markdown("**✅ Excel·leix quan:**")
            st.markdown("""
- Crashes en V (caiguda ràpida → recuperació ràpida)
- Mercats volàtils amb pànics puntuals
- Correccions profundes > 20%
- Ex: Crash COVID (Mar 2020), Flash Crash d'Abril 2025
""")
        st.markdown("")
        with st.container(border=True):
            st.markdown("**⚠️ Limitacions:**")
            st.markdown("""
- Pot estar molts mesos en cash durant mercats alcistes
- Si el crash dura molt (BEAR prolongat), pot acumular pèrdues
- Requereix paciència — no és un bot de rotació ràpida
""")

    st.divider()

    # ── Bot 10: Trend Momentum ─────────────────────────────────────────────────
    st.markdown("### 📈 Bot 10 — Trend Momentum")

    left, right = st.columns([3, 2])
    with left:
        st.markdown("""
**Filosofia:** *Compra el pull-back dins d'una tendència alcista.*

Aquest bot opera quan el mercat va bé. Busca accions que estan en tendència
alcista però que han tingut una correcció moderada (un "respir"). Entra en el
moment en què la tendència es reprèn.
""")

        with st.expander("📋 Condicions d'entrada (totes han de complir-se)"):
            st.markdown("""
1. **Mercat alcista confirmat:** `SXR8.DE` per sobre de la seva **SMA200** — tendència
   global positiva.
2. **Acció en tendència:** preu de l'acció per sobre de la seva **SMA50** — tendència
   individual positiva.
3. **Pull-back moderat:** RSI de l'acció entre **40 i 62** — ha baixat però no
   ha entrat en territori de pànic.
4. **Momentum recuperant:** RSI actual més alt que fa **3 dies** — la correcció
   s'ha aturat i el comprador torna.
""")

        with st.expander("🚪 Condicions de sortida (per ordre de prioritat)"):
            st.markdown("""
1. **Stop catastròfic:** si l'acció cau un **15%** des del cost → sortida immediata.
2. **Ruptura de tendència:** si l'acció tanca per sota de la **SMA50** durant
   **3 dies consecutius** → la tendència s'ha trencat, sortim.
3. **Stop seguidor:** **22%** des del màxim assolit — prou ample per aguantar
   oscil·lacions normals però que tanca si el mercat gira de veritat.
4. **Sortida temporal:** màxim **60 dies** en posició, si mai ha estat en guanys.
""")

    with right:
        with st.container(border=True):
            st.markdown("**✅ Excel·leix quan:**")
            st.markdown("""
- Mercats alcistes graduals (BULL continu)
- Correccions del 10–15% seguides de recuperació
- Anys com 2023, 2024 (S&P 500 pujant amb correccions sanes)
- Sectors en tendència clara (tecnologia, indústria)
""")
        st.markdown("")
        with st.container(border=True):
            st.markdown("**⚠️ Limitacions:**")
            st.markdown("""
- No opera durant crashes (el mercat cau per sota de la SMA200)
- En mercats laterals pot entrar i sortir massa (whipsaw)
- Menys efectiu en períodes de molta volatilitat
""")

    st.divider()

    # ── Combined Strategy ──────────────────────────────────────────────────────
    st.markdown("### 🔀 Per Què Combinar-los?")

    st.markdown("""
Els dos bots estan dissenyats per ser **complementaris**: cadascun excel·leix
exactament quan l'altre no opera.
""")

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        with st.container(border=True):
            st.markdown(
                f"🔴 **CRASH**",
                unsafe_allow_html=True,
            )
            st.markdown("🤖 RSI Compounder **ACTIU**")
            st.markdown("📈 Trend Momentum en cash")

    with c2:
        with st.container(border=True):
            st.markdown("🟡 **CORRECCIÓ**")
            st.markdown("🤖 RSI Compounder en cash")
            st.markdown("📈 Trend Momentum **ACTIU**")

    with c3:
        with st.container(border=True):
            st.markdown("🟢 **BULL**")
            st.markdown("🤖 RSI Compounder en cash")
            st.markdown("📈 Trend Momentum **ACTIU**")

    with c4:
        with st.container(border=True):
            st.markdown("⬛ **BEAR**")
            st.markdown("🤖 RSI Compounder en cash")
            st.markdown("📈 Trend Momentum en cash")
            st.caption("Ambdós protegeixen capital")

    st.markdown("")

    with st.container(border=True):
        st.markdown("#### 🎯 El raonament clau")
        st.markdown("""
**Cobertura de règims:** sense la combinació, durant un any purament alcista el
RSI Compounder estaria en cash gairebé tot el temps (pocs crashes). Amb el Trend
Momentum actiu en paral·lel, el capital segueix generant rendiment.

**Cash eficient:** quan el RSI Compounder no troba oportunitats (mercat tranquil),
el seu efectiu virtual no fa res. El Trend Momentum el "cobreix" en aquest règim.
A l'inrevés, durant un crash el Trend Momentum para i el RSI Compounder treballa.

**Risc diversificat:** mai els dos bots estan simultàniament en posicions
agressives. En un crash, el Trend Momentum ja haurà sortit de les seves posicions
(SMA50 trencada) just quan el RSI Compounder comença a entrar.

Els resultats detallats dels backtests es poden consultar a la pestanya **📊 Backtest**.
""")

    st.divider()

    # ── FAQ ────────────────────────────────────────────────────────────────────
    st.markdown("### ❓ Preguntes Freqüents")

    with st.expander("Puc perdre diners reals?"):
        st.markdown("""
**Ara mateix, no.** Estem operant amb un compte de *paper trading* (diners virtuals).
Tot funciona igual que un compte real, però les operacions no afecten diners reals.
Quan tinguem confiança en els resultats, decidirem conjuntament si passem a compte real.
""")

    with st.expander("Qui controla el bot? Puc aturar-lo?"):
        st.markdown("""
El bot l'administra en Ferran. Qualsevol de vosaltres pot:
- **Veure el rendiment** en temps real en aquest dashboard.
- **Canviar l'estratègia** des de la pestanya Paper o En Viu (selector d'estratègia).
- **Deshabilitar el trading en viu** des de l'interruptor de la pestanya En Viu.

Si hi ha qualsevol problema, parleu-ho directament amb en Ferran.
""")

    with st.expander("Com es calculen les comissions?"):
        st.markdown("""
IBKR cobra comissions per cada operació executada. Per a accions europees
(Xetra), la comissió és d'aproximadament **€1,25 per operació** + 0,05% del
valor. Per a accions americanes, ~**$1,00** per operació.

Al dashboard, la pestanya de *Operacions IBKR* mostra les **comissions reals**
carregades per IBKR per a cada transacció — no una estimació, sinó el cost real.
""")

    with st.expander("Amb quina freqüència opera el bot?"):
        st.markdown("""
El bot s'executa **una vegada al dia**, típicament al matí abans de l'obertura
dels mercats europeus (09:00–09:30 CET). Analitza totes les accions de l'univers
i col·loca les ordres que compleixen les condicions.

En règims de calma (BULL sense pull-backs significatius), pot passar dies o
setmanes sense operar. En moments de crash, pot entrar en diverses posicions
el mateix dia.
""")

    with st.expander("Per què UCITS ETFs i no ETFs americans (SPY, QQQ)?"):
        st.markdown("""
La regulació europea (**PRIIPs**) restringeix als inversors retail de la UE la
compra d'ETFs americans com SPY o QQQ. Els ETFs **UCITS** (com SXR8.DE, SXRV.DE)
són equivalents europeus que repliquen els mateixos índexs i estan aprovats per
a inversors de la UE. Les accions individuals (AAPL, MSFT, etc.) **no** estan
afectades per aquesta restricció.
""")
