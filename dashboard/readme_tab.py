"""README / Summary tab — explains the project to Antonio and Adria."""
from __future__ import annotations

import streamlit as st


# ── Sector labels ──────────────────────────────────────────────────────────────
# Each entry: (ticker, full_name)

_US_SECTORS: dict[str, list[tuple[str, str]]] = {
    "💻 Tecnologia / Creixement": [
        ("AAPL",  "Apple Inc"),
        ("MSFT",  "Microsoft Corp"),
        ("GOOGL", "Alphabet Inc"),
        ("AMZN",  "Amazon.com Inc"),
        ("META",  "Meta Platforms"),
        ("NVDA",  "Nvidia Corp"),
    ],
    "🔌 Semiconductors": [
        ("AVGO",  "Broadcom Inc"),
        ("MU",    "Micron Technology"),
        ("QCOM",  "Qualcomm"),
    ],
    "🏦 Finances": [
        ("JPM",   "JPMorgan Chase & Co"),
        ("V",     "Visa Inc"),
        ("BAC",   "Bank of America Corp"),
        ("GS",    "Goldman Sachs Group"),
    ],
    "🏥 Salut": [
        ("UNH",   "UnitedHealth Group"),
        ("JNJ",   "Johnson & Johnson"),
        ("PFE",   "Pfizer Inc"),
        ("LLY",   "Eli Lilly & Co"),
    ],
    "⛽ Energia": [
        ("XOM",   "Exxon Mobil Corp"),
        ("CVX",   "Chevron Corp"),
    ],
    "🏗 Industrials": [
        ("CAT",   "Caterpillar Inc"),
        ("HON",   "Honeywell International"),
    ],
    "🛒 Consum bàsic": [
        ("PG",    "Procter & Gamble"),
        ("KO",    "Coca-Cola Co"),
        ("WMT",   "Walmart Inc"),
        ("COST",  "Costco Wholesale"),
    ],
    "🛍 Consum discrecional": [
        ("HD",    "Home Depot Inc"),
        ("NKE",   "Nike Inc"),
        ("DIS",   "Walt Disney Co"),
        ("BKNG",  "Booking Holdings"),
        ("UBER",  "Uber Technologies"),
    ],
    "💼 Enterprise Software": [
        ("SHOP",  "Shopify Inc"),
        ("CRM",   "Salesforce Inc"),
        ("NOW",   "ServiceNow Inc"),
    ],
}

_EU_SECTORS: dict[str, list[tuple[str, str]]] = {
    "💻 Tecnologia / Semis": [
        ("ASML.AS", "ASML Holding NV"),
        ("SAP.DE",  "SAP SE"),
        ("IFX.DE",  "Infineon Technologies AG"),
    ],
    "✈️ Aeroespacial / Luxe": [
        ("AIR.PA",  "Airbus SE"),
        ("MC.PA",   "LVMH Moët Hennessy"),
        ("RMS.PA",  "Hermès International"),
        ("OR.PA",   "L'Oréal SA"),
    ],
    "⚙️ Industrials / Defensa": [
        ("SIE.DE",  "Siemens AG"),
        ("RHM.DE",  "Rheinmetall AG"),
    ],
    "🏦 Finances": [
        ("BNP.PA",  "BNP Paribas"),
        ("ALV.DE",  "Allianz SE"),
    ],
    "🏥 Salut / Farmàcia": [
        ("NOVN.SW", "Novartis AG"),
        ("BAYN.DE", "Bayer AG"),
    ],
    "🚗 Automòbil": [
        ("BMW.DE",  "BMW Group"),
    ],
    "🛒 Consum bàsic": [
        ("NESN.SW", "Nestlé SA"),
    ],
    "⛽ Energia": [
        ("TTE.PA",  "TotalEnergies SE"),
    ],
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
            st.markdown("**Trading 212**")
            st.caption(
                "Broker europeu sense comissions per a accions i ETFs. "
                "Accés directe a borses europees i americanes via API REST."
            )
    with col2:
        with st.container(border=True):
            st.markdown("**Comptes**")
            st.markdown("**Paper (pràctica) + En Viu (real)**")
            st.caption(
                "Paper: diners virtuals per validar estratègies sense risc. "
                "En Viu: compte real activable des de la pestanya En Viu."
            )
    with col3:
        with st.container(border=True):
            st.markdown("**Execució**")
            st.markdown("**Automàtica · Diària**")
            st.caption(
                "El bot s'executa cada dia laborable abans de l'obertura del mercat. "
                "Les ordres es col·loquen automàticament via Trading 212 API."
            )

    st.markdown("""
**Com funciona el pressupost virtual?**
Cada compte T212 (paper o en viu) té un saldo total. El sistema divideix
aquest saldo entre els bots actius del propietari del compte segons el **%
d'assignació** configurat a la pestanya ⚖️ Assignació (per defecte 50 / 50).
Cada bot gestiona el seu propi efectiu i posicions de manera independent —
el rendiment de cada estratègia és mesurable per separat.
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
        for sector, stocks in _US_SECTORS.items():
            cols = st.columns([2, 5])
            cols[0].markdown(f"**{sector}**")
            cols[1].markdown("  ·  ".join(name for _, name in stocks))

    with tab_eu:
        st.caption(
            "Accions europees en diverses borses (Xetra, Euronext, SIX Swiss). "
            "La majoria en EUR; NESN.SW i NOVN.SW en CHF."
        )
        for sector, stocks in _EU_SECTORS.items():
            cols = st.columns([2, 5])
            cols[0].markdown(f"**{sector}**")
            cols[1].markdown("  ·  ".join(name for _, name in stocks))

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
                "Retrocés dins una tendència alcista. "
                "El Trend Momentum captura el rebot. "
                "El RSI Compounder roman en efectiu."
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
    st.markdown("### 🤖 RSI Compounder (bots 7 / 17)")
    st.caption("Bot 7 = paper · Bot 17 = en viu (activable des de la pestanya En Viu)")

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
4. **No massa calenta:** RSI actual < 65 per evitar entrar en accions ja molt recuperades.
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

        with st.expander("📈 Scale-in (màxim de posicions assolit)"):
            st.markdown("""
Quan el bot ja té **10 posicions obertes** però encara queda efectiu disponible,
en lloc d'esperar ociós reavalia totes les posicions actuals amb el senyal d'entrada.
**Totes** les que tornen a complir el senyal (han tornat a entrar en sobrevenda
i es recuperen) reben una compra fins a arribar al seu pes objectiu (**6,7%**
del capital per acció). Les compres es prioritzen per la força del senyal i el
límit de 5 operacions/dia reparteix la resta en dies següents. Així el capital
addicional es desplega en les oportunitats reals de la cartera sense obrir noves
posicions.
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
- Pot estar molts mesos en efectiu durant mercats alcistes
- Si el crash dura molt (BEAR prolongat), pot acumular pèrdues
- Requereix paciència — no és un bot de rotació ràpida
""")

    st.divider()

    # ── Bot 10: Trend Momentum ─────────────────────────────────────────────────
    st.markdown("### 📈 Trend Momentum (bots 10 / 20)")
    st.caption("Bot 10 = paper · Bot 20 = en viu (activable des de la pestanya En Viu)")

    left, right = st.columns([3, 2])
    with left:
        st.markdown("""
**Filosofia:** *Compra el retrocés dins d'una tendència alcista.*

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
3. **Retrocés moderat:** RSI de l'acció entre **40 i 62** — ha baixat però no
   ha entrat en territori de pànic.
4. **Momentum recuperant:** RSI actual més alt que fa **3 dies** — la correcció
   s'ha aturat i el comprador torna.
5. **Sense resultats propers:** s'eviten accions amb **presentació de resultats**
   en els propers o passats **7 dies** (risc d'un gap brusc).
""")

        with st.expander("🚪 Condicions de sortida (per ordre de prioritat)"):
            st.markdown("""
1. **Stop catastròfic:** si l'acció cau un **15%** des del cost → sortida immediata.
2. **Ruptura de tendència:** si l'acció tanca per sota de la **SMA50** durant
   **3 dies consecutius** → la tendència s'ha trencat, sortim.
3. **Stop seguidor:** **8% des del màxim en EUR** — mesurat en euros (divisa del
   compte) per capturar el guany real independentment de les fluctuacions del canvi
   USD/EUR. Prou ajustat per no deixar escapar guanys en correccions ràpides.
4. **Sortida temporal:** màxim **60 dies** en posició, si mai ha estat en guanys.
""")

        with st.expander("📈 Scale-in (màxim de posicions assolit)"):
            st.markdown("""
Quan el bot ja té **10 posicions obertes** però queda efectiu disponible,
reavalia totes les posicions actuals amb el senyal d'entrada complet
(SMA50, RSI 40–62, momentum creixent, sense resultats propers, mercat alcista).
**Totes** les que el compleixen reben una compra fins a arribar al seu pes
objectiu (**10%** del capital per acció) — mai més del 10%, i les que ja hi són
no es toquen. Les compres es prioritzen per la força del senyal i el límit de
5 operacions/dia reparteix la resta en dies següents.
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
- Menys eficaç en períodes de molta volatilitat
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
            st.markdown("🔴 **CRASH**")
            st.markdown("🤖 RSI Compounder **ACTIU**")
            st.markdown("📈 Trend Momentum en efectiu")

    with c2:
        with st.container(border=True):
            st.markdown("🟡 **CORRECCIÓ**")
            st.markdown("🤖 RSI Compounder en efectiu")
            st.markdown("📈 Trend Momentum **ACTIU**")

    with c3:
        with st.container(border=True):
            st.markdown("🟢 **BULL**")
            st.markdown("🤖 RSI Compounder en efectiu")
            st.markdown("📈 Trend Momentum **ACTIU**")

    with c4:
        with st.container(border=True):
            st.markdown("⬛ **BEAR**")
            st.markdown("🤖 RSI Compounder en efectiu")
            st.markdown("📈 Trend Momentum en efectiu")
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

    # ── Capital Allocation ─────────────────────────────────────────────────────
    st.markdown("### ⚖️ Assignació de Capital")

    st.markdown("""
Cada propietari de compte pot distribuir el seu saldo de T212 entre els seus dos
bots en qualsevol proporció que sumi 100%. La configuració es fa a la pestanya
**⚖️ Assignació** del dashboard.
""")

    col_a, col_b = st.columns(2)
    with col_a:
        with st.container(border=True):
            st.markdown("**Paper Trading**")
            st.caption("Sempre dividit equitativament entre els bots actius. No configurable.")
    with col_b:
        with st.container(border=True):
            st.markdown("**En Viu**")
            st.caption(
                "Configurable per propietari. Per defecte 50% / 50%. "
                "El canvi té efecte en la propera execució diària del bot."
            )

    st.markdown("""
**Com funciona el pressupost:** el bot multiplica el saldo total dipositat
al compte T212 pel seu % assignat per obtenir el seu pressupost. En funció
de quant d'aquest pressupost ja té invertit en posicions obertes, decideix
si té prou efectiu disponible per obrir noves posicions o fer scale-in.
""")

    st.divider()

    # ── Strategy Lab ───────────────────────────────────────────────────────────
    st.markdown("### 🧪 Laboratori d'Estratègies (IA)")

    st.markdown("""
El **Laboratori d'Estratègies** és una capa d'intel·ligència artificial (Claude)
que analitza l'historial de les posicions tancades i proposa ajustos numèrics als
paràmetres dels bots (RSI, stops, mides de posició, etc.).
""")

    col_l, col_r = st.columns([3, 2])
    with col_l:
        with st.expander("Com funciona el cicle d'aprenentatge"):
            st.markdown("""
1. **Anàlisi:** l'agent llegeix les posicions tancades reals i fa backtests amb
   paràmetres alternatius per mesurar si millorarien el rendiment.
2. **Proposta:** si troba una millora, crea una proposta amb el paràmetre,
   el valor actual, el nou valor i la justificació.
3. **Aprovació:** l'administrador revisa la proposta a la pestanya
   🧪 Laboratori i l'accepta o rebutja.
4. **Aplicació:** si s'accepta, el YAML de configuració s'actualitza
   automàticament i el bot utilitza el nou valor en la propera execució.
5. **Seguiment:** 30 i 90 dies després, el sistema mesura si el P&L
   va millorar realment amb el canvi.
""")
    with col_r:
        with st.container(border=True):
            st.markdown("**Garanties de seguretat:**")
            st.markdown("""
- La IA **mai** opera directament
- Tots els canvis passen per aprovació humana
- Hi ha rangs màxims per a cada paràmetre
- Cada proposta inclou validació walk-forward
""")

    st.divider()

    # ── FAQ ────────────────────────────────────────────────────────────────────
    st.markdown("### ❓ Preguntes Freqüents")

    with st.expander("Puc perdre diners reals?"):
        st.markdown("""
**En mode paper, no.** Els bots paper (7, 9, 10, 12) operen amb diners virtuals —
tot funciona igual que un compte real però les operacions no afecten diners reals.

**En mode en viu, sí.** Els bots en viu (17, 20, 19, 22) operen amb diners reals
del compte T212. Per activar-los cal encendre l'interruptor a la pestanya
**💶 En Viu**. Per defecte estan **desactivats**.

Quan tingueu confiança en els resultats del paper, podeu decidir conjuntament
si activeu el compte en viu.
""")

    with st.expander("Qui controla el bot? Puc aturar-lo?"):
        st.markdown("""
El bot l'administra en Ferran. Qualsevol de vosaltres pot:
- **Veure el rendiment** en temps real en aquest dashboard.
- **Canviar l'estratègia** des de la pestanya Paper o En Viu (selector d'estratègia).
- **Deshabilitar el trading en viu** des de l'interruptor de la pestanya En Viu.
- **Canviar l'assignació de capital** des de la pestanya ⚖️ Assignació.

Les ordres es col·loquen directament al compte de Trading 212 via API. Si hi ha
qualsevol problema, parleu-ho directament amb en Ferran.
""")

    with st.expander("Com es calculen les comissions?"):
        st.markdown("""
Trading 212 **no cobra comissions** per a accions i ETFs. El cost real ve de
la conversió de divises quan operem accions americanes (en USD):

- **Accions europees (EUR):** cost **0 €** per operació.
- **Accions americanes (USD):** **0,15%** del valor de l'operació per la
  conversió EUR → USD. Per exemple, una operació de €10.000 en AAPL costa ~€15.

Al dashboard, les comissions mostrades reflecteixen aquesta taxa de conversió,
extreta directament de la resposta de l'API de Trading 212.
""")

    with st.expander("Amb quina freqüència opera el bot?"):
        st.markdown("""
El bot s'executa **una vegada al dia**, típicament al matí abans de l'obertura
dels mercats europeus (09:00–09:30 CET). Analitza totes les accions de l'univers
i col·loca les ordres que compleixen les condicions.

En règims de calma (BULL sense correccions significatives), el RSI Compounder pot
passar dies o setmanes sense obrir posicions noves. En moments de crash, pot entrar
en diverses posicions el mateix dia.

Quan un bot ja té **10 posicions obertes** (màxim permès) però encara té efectiu
disponible, activa el **scale-in**: reavalia les posicions actuals i afegeix
capital a **totes** les que tornen a complir el senyal d'entrada, fins al seu pes
objectiu per acció (10% Trend Momentum · 6,7% RSI Compounder). Es prioritzen per
força del senyal i es respecta el límit de 5 operacions/dia. Així el capital mai
queda ociós innecessàriament.
""")

    with st.expander("Qui pot canviar l'assignació de capital?"):
        st.markdown("""
Només l'**administrador** (Ferran) pot modificar i desar els percentatges
d'assignació des de la pestanya ⚖️ Assignació. Els altres usuaris poden veure
la configuració actual però no editar-la.

El canvi té efecte en la **propera execució diària** del bot — no hi ha
rebalanceig immediat de les posicions obertes.
""")

    with st.expander("Per què el stop del Trend Momentum és tan ajustat (8%)?"):
        st.markdown("""
El stop seguidor del Trend Momentum es **mesura en EUR** (la divisa del compte),
no en la divisa nativa de l'acció. Això és important per a accions americanes:

- Una acció americana pot caure un **12% en USD** però, si el dòlar s'ha
  enfortit alhora, la pèrdua en EUR pot ser molt menor (o fins i tot un guany).
- Si mesuréssim el stop en USD, podríem sortir d'una posició que en realitat
  és positiva en EUR.

Per tant, el **8% és la caiguda real des del màxim en euros** — és el guany
real que estem protegint. Un 8% en EUR és equivalent a un stop d'uns
12–15% en USD depenent de la paritat del moment.
""")

    with st.expander("Per què UCITS ETFs i no ETFs americans (SPY, QQQ)?"):
        st.markdown("""
La regulació europea (**PRIIPs**) restringeix als inversors retail de la UE la
compra d'ETFs americans com SPY o QQQ. Els ETFs **UCITS** (com SXR8.DE, SXRV.DE)
són equivalents europeus que repliquen els mateixos índexs i estan aprovats per
a inversors de la UE. Les accions individuals (AAPL, MSFT, etc.) **no** estan
afectades per aquesta restricció.
""")
