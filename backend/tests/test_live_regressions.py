"""Regression tests from the first live run (2026-09-24, Finnhub news).

Every headline here was handled wrongly before the fix: linked to the wrong
company, labelled a takeover, or counted as a signal although it describes no
company event. The universe is the offline subset plus the real SEC entries of
every company that was wrongly linked (fixtures/reference/live_regression_companies.json).
Finnhub /company-news tags arrive as weak hints (score 0.0), exactly as the
adapter produces them.
"""

import pytest

from catalystedge.fixtures import load_json
from catalystedge.ml.sentiment import LexiconSentiment
from catalystedge.pipeline.classify import classify
from catalystedge.pipeline.noise import non_event_reason
from catalystedge.pipeline.ticker_link import Universe, link_tickers


def _universe() -> Universe:
    live = load_json("reference/live_regression_companies.json")
    tickers = {row["ticker"] for row in live.values()}
    rows = dict(live)
    for i, row in enumerate(load_json("reference/company_tickers.json").values()):
        if row["ticker"] not in tickers:
            rows[str(100000 + i)] = row
    return Universe.from_sec_company_tickers(rows)


U = _universe()
LEX = LexiconSentiment()


def weak(*symbols):
    """Finnhub /company-news tags: only the queried symbol, as a weak hint."""
    return dict.fromkeys(symbols, 0.0)


def link(headline, provider=None):
    return link_tickers(headline, provider or {}, U).symbols


def events(headline, provider=None):
    mentions = link_tickers(headline, provider or {}, U).mentions
    return {e.symbol: e for e in classify(headline, mentions, LEX.predict([headline])[0])}


# ----------------------------------------------------------------------------- 1. ticker linking

@pytest.mark.parametrize(
    ("headline", "provider", "expected"),
    [
        # ordinary words were read as company names: Vision, Strategy
        ("Meta CEO Mark Zuckerberg Admits AI Moved Faster Than Metaverse Vision, Driving Strategy Shift",
         weak("META"), ["META"]),
        # Where -> Where Food Comes From; Stock -> Stock Yards Bancorp; NVDA from a company-news query
        ("Where Will Broadcom Stock Be in 5 Years?", weak("NVDA"), ["AVGO"]),
        ("Apple (AAPL) Stock Could Be 32% Overpriced After Fresh AI Cost Warnings", weak("AAPL"), ["AAPL"]),
        # Gemini (the Google model) -> Gemini Space Station
        ("GOOGL Stock Steadies After Worst Drop In A Month As DeepMind Chief Says Gemini 4 Is Coming 'Much Earlier'",
         weak("META"), ["GOOGL"]),
        ("What Could Alphabet (GOOGL) Gain From Bringing Personal Finance Into Gemini?", {}, ["GOOGL"]),
        # Here's -> Here Group
        ("Prediction: Here's What $10,000 Invested in AbbVie Stock With Dividends Reinvested Could Be Worth in a "
         "Decade", weak("NVDA"), ["ABBV"]),
        ("I've Held Nvidia for Nearly 3 Years. Here's Whether I'd Still Buy Today.", {}, ["NVDA"]),
        # CRE (commercial real estate) and Construction -> Construction Partners
        ("Data Center Boom Pushes CRE Construction Costs Higher", weak("AMZN", "META"), []),
        # Free -> Free Flow
        ("Mark Zuckerberg Says Muse Will Stay Free for 'a Huge Number of Tokens': Walmart, Sephora, Best Buy and "
         "More Sign on to Power Shopping", weak("META"), ["WMT", "BBY"]),
        ("Intel (INTC) Stock Looks Fully Priced After Its 293% Run", weak("META"), ["INTC"]),
        ("Why Fastly Stock Jumped Today", weak("NVDA"), ["FSLY"]),
        # Trump -> Trump Media
        ("US and Indian top diplomats discuss Russia sanctions bill signed by Trump - Reuters", {}, []),
        # Cross-border -> Cross Timbers Royalty Trust
        ("41Caijing and Cifnews (Xiamen) Cross-border E-commerce Co., Ltd Successfully Host Bringing New Products "
         "to the World Event on Crowdfunding and Global Growth", weak("META"), []),
        ("Steve Eisman Says AI CEOs Are Faking the ‘Doomsday Crisis’ — Anthropic May Be Speeding Up Anyway",
         weak("META"), []),
        ("Is an AMD Stock Split Coming Now That the Shares Have Topped $600?", weak("NVDA"), ["AMD"]),
        # Connect -> Connect Biopharma; News -> News Corp
        ("Meta Stock Expected to be 'Flat to Down' on 'Connect' Keynote, Says Gene Munster: Muse is 'Bad News' for "
         "Google, Says Analyst", weak("GOOGL", "META"), ["META", "GOOGL"]),
        # On (title case) -> On Holding
        ("Unity Software Stock Surges Nearly 5% Overnight On Meta’s New VR, AI Hardware Push", weak("META"),
         ["U", "META"]),
        # Nasdaq (the index) -> Nasdaq Inc
        ("Anthropic Could Join the Nasdaq-100 Only 15 Trading Days After Its IPO, Just Like SpaceX Did",
         weak("NVDA"), []),
        # Dow (the index) -> Dow Inc
        ("Dow, S&P 500, Nasdaq Futures Dip As Bond Yields Bite And Trump-Xi Meet Grabs Spotlight: Why META, MCD, "
         "GOOG, U Stocks Are Trending", weak("META"), ["META", "MCD", "GOOGL"]),
        ("Meta Platforms Unveils Muse AI Agent, Expands Smart Glasses and VR Push", weak("META"), ["META"]),
        ("HSBC sends blunt message to Netflix stock investors", weak("GOOGL"), ["HSBC", "NFLX"]),
        # Income -> Income Opportunity Realty
        ("AMZY: Weekly Income From Amazon's $496 Billion AWS Backlog", weak("AMZN"), ["AMZN"]),
        # Game-Changer -> Game Your Game
        ("META Stock Slips After Connect Launches, But Analysts Believe Muse AI Is 'A Game-Changer'", weak("META"),
         ["META"]),
        # Paid -> Paid Inc
        ("Amazon Blocked Meta's AI Shopping Agent. Shopify Welcomed It -- and Gets Paid on Every Checkout.",
         weak("NVDA", "META"), ["AMZN", "META", "SHOP"]),
        ("Dow Jones Futures Fall After Soaring Yields Hit Stocks, But Palantir Flashes Buy Signal; Trump-Xi On Tap",
         weak("META"), ["PLTR"]),
        # People -> People Inc; Three -> Three Lions Acquisition
        ("Harvard's Judgment Professor: Numbers Don't Make Decisions; People Do", weak("NVDA"), []),
        ("Three Lesser-Known But Powerful 401(k) Features", weak("NVDA"), []),
        # Monster -> Monster Beverage; Hold -> Hold Me Ltd
        ("2 Monster Dividend Stocks to Buy Now and Hold for Decades", weak("NVDA"), []),
        # Commerce -> Commerce Bancshares
        ("Meta’s Agent-Commerce Layer Just Went Live. The Settlement Rails Were Built for This.", weak("META"),
         ["META"]),
        # second live run: Su -> SU Group; Opus -> Opus Genetics; Global gas -> Global Gas Corp
        ("CPUs Are Hot Again: AMD’s Lisa Su Says the Real Demand Wave Hasn’t Even Started", {}, ["AMD"]),
        ("Anthropic Unveils More Cost-Efficient Opus 5.5 Model Before IPO", {}, []),
        ("Global gas market pricing prolonged tightness due to Iran war, IGU executive says - Reuters", {}, []),
        ("Alphabet’s Spark Agentic AI Will Beat Meta’s Muse in the Long Run", {}, ["GOOGL", "META"]),
        ("AMD: This Uptrend Is Far From Over", {}, ["AMD"]),
        # "Apple" is shared with Apple Hospitality REIT; the famous company wins
        ("Meta Puts Apple Firmly in Its Sights With New VR Glasses", weak("AAPL"), ["META", "AAPL"]),
    ],
)
def test_live_headline_links(headline, provider, expected):
    assert link(headline, provider) == expected


@pytest.mark.parametrize(
    ("headline", "wrong"),
    [
        ("SNAP Stock Hits Over 1-Month Low Amid SPECS Backlash — Meta Unveils Cheaper Glasses", "SYBT"),
        ("Tesla Makes Brilliant Supercharger Move but BYD Is Closer in the Mirror Than It Appears", "BRLT"),
        ("Dolby and Meta Expand Dolby Atmos and Dolby Vision Across Meta's Ecosystem", "ATO"),
        ("OpenAI's GPT-6 Just Got 50% Cheaper — Box CEO Says AI Agent Opportunity Could 'Dramatically' Expand",
         "GGRPY"),
        ("BIO-key Receives FIDO Alliance Full Certification for Passkey Authentication", "BIO"),
        ("Tracking Ole Andreas Halvorsen's Viking Global Portfolio - Q2 2026 Update", "QTWO"),
        # "AI Era" is a headline phrase, not AI Era Corp
        ("Jamf Takes Apple Management into the AI Era at JNUC 2026", "AERA"),
    ],
)
def test_live_headline_does_not_link_wrong_company(headline, wrong):
    assert wrong not in link(headline, weak("META", "NVDA"))


def test_company_news_tag_alone_never_links():
    """Finnhub returns loosely related articles for a company-news query; the tag alone is not enough."""
    assert link("Three Lesser-Known But Powerful 401(k) Features", weak("NVDA")) == []
    r = link_tickers("Nvidia once rattled IonQ stock. Now it plans to install IonQ tech", weak("NVDA"), U)
    assert "NVDA" in r.symbols   # named in the headline: the tag corroborates it


def test_explicit_provider_tag_still_links():
    assert link("Chipmaker shares jump on upbeat outlook", {"NVDA": 0.95}) == ["NVDA"]


# ----------------------------------------------------------------------------- 2. M&A wording

@pytest.mark.parametrize(
    "headline",
    [
        "Dow Jones Futures Fall After Soaring Yields Hit Stocks, But Palantir Flashes Buy Signal; Trump-Xi On Tap",
        "2 Monster Dividend Stocks to Buy Now and Hold for Decades",
        "Stifel Upgrades Microsoft to Buy, Raises Price Target to $575",
        "Is It Time to Buy Palantir Stock?",
        "Microsoft: Buy This Early Recovery",
        "Tesla makes $122M on Bitcoin without buying a single coin",
        "Intel buys back $2 billion of shares",
    ],
)
def test_buy_is_never_a_takeover(headline):
    assert all(e.event_type != "m_and_a_target" for e in events(headline, weak("META")).values())


def test_real_deal_wording_is_still_a_takeover():
    assert events("Abbott to acquire Hologic for $15 billion in cash")["HOLX"].event_type == "m_and_a_target"
    assert events("Hologic agrees to be acquired by Abbott at 30% premium")["HOLX"].event_type == "m_and_a_target"
    assert events("Hologic receives takeover bid from Abbott")["HOLX"].event_type == "m_and_a_target"


# ----------------------------------------------------------------------------- 2b. other false signals

@pytest.mark.parametrize(
    ("headline", "symbol", "not_type"),
    [
        # "Upgrade" in the engineering sense is not an analyst upgrade
        ("Google Is Helping Upgrade Southern’s Nuclear Plants. Can 96 MW Ease Its AI Power Bottleneck?", "GOOGL",
         "upgrade"),
        ("Google Goes Nuclear With Power Upgrade Agreement in Georgia", "GOOGL", "upgrade"),
        # "Top Analyst Forecasts" is not "tops forecasts"
        ("Microsoft To Rally More Than 15%? Here Are 10 Top Analyst Forecasts For Wednesday", "MSFT",
         "earnings_beat"),
        # a price target is not company guidance
        ("AI Alliances And Raised Targets Could Be A Game Changer For ServiceNow (NOW)", "NOW", "guidance_raise"),
        ("Crypto's Next Catalyst Could Be Tokenized Stocks: Clear Street Raises Bullish, Coinbase Targets", "BLSH",
         "guidance_raise"),
    ],
)
def test_live_false_catalysts(headline, symbol, not_type):
    e = events(headline).get(symbol)
    assert e is None or e.event_type != not_type, e.reasons


def test_broker_is_not_the_upgraded_company():
    ev = events("Stifel Upgrades Microsoft to Buy, Raises Price Target to $575")
    assert ev["MSFT"].event_type == "upgrade" and ev["MSFT"].is_signal_eligible
    assert not ev["SF"].is_signal_eligible


def test_price_target_lift_is_an_upgrade_not_guidance():
    e = events("Meta Could Ride AI’s ‘Third S-Curve’ As Personal Agent Muse Takes Off, Says Cantor — KeyBanc Lifts "
               "Target To $900")["META"]
    assert e.event_type == "upgrade"


# ----------------------------------------------------------------------------- 3. non-event filter

@pytest.mark.parametrize(
    ("headline", "reason"),
    [
        ("2 Monster Dividend Stocks to Buy Now and Hold for Decades", "listicle"),
        ("3 Reasons to Buy Brookfield Renewable Before September Ends", "listicle"),
        ("Billionaire Chase Coleman’s Top 2 New AI Stock Picks", "listicle"),
        ("Microsoft To Rally More Than 15%? Here Are 10 Top Analyst Forecasts For Wednesday", "listicle"),
        ("Hyperliquid Could Be About to Get U.S. Regulatory Approval. Is HYPE Worth Buying Now?",
         "stock_picking_advice"),
        ("Apple (AAPL) Stock Could Be 32% Overpriced After Fresh AI Cost Warnings", "stock_picking_advice"),
        ("Intel (INTC) Stock Looks Fully Priced After Its 293% Run", "stock_picking_advice"),
        ("I've Held Nvidia for Nearly 3 Years. Here's Whether I'd Still Buy Today.", "stock_picking_advice"),
        ("Dow Jones Futures Fall After Soaring Yields Hit Stocks, But Palantir Flashes Buy Signal; Trump-Xi On Tap",
         "stock_picking_advice"),
        ("Is Autozone a Buy After Its Latest Earnings Report?", "stock_picking_advice"),
        ("Where Will Broadcom Stock Be in 5 Years?", "long_range_speculation"),
        ("Prediction: Here's What $10,000 Invested in AbbVie Stock With Dividends Reinvested Could Be Worth in a "
         "Decade", "long_range_speculation"),
        ("Why Did META, MRNA, OKTA Stocks Surge To 52-Week Highs Today?", "price_move_only"),
        ("Why Fastly Stock Jumped Today", "price_move_only"),
        ("SNAP Stock Hits Over 1-Month Low Amid SPECS Backlash — Meta Unveils Cheaper Glasses And Musk Fuels AI FOMO",
         "price_move_only"),
        ("GOOGL Stock Steadies After Worst Drop In A Month As DeepMind Chief Says Gemini 4 Is Coming 'Much Earlier'",
         "price_move_only"),
        ("Unity Software Stock Surges Nearly 5% Overnight On Meta’s New VR, AI Hardware Push", "price_move_only"),
        ("META Stock Slips After Connect Launches, But Analysts Believe Muse AI Is 'A Game-Changer'",
         "price_move_only"),
        ("Alphabet Inc. (GOOG) Dips More Than Broader Market: What You Should Know", "price_move_only"),
        ("Why Instacart (CART) Shares Are Trading Lower Today", "price_move_only"),
        ("AMD Is Up 187% This Year and Nvidia Is Up 22%. Prediction: Nvidia Wins the Next 12 Months.",
         "price_move_only"),
        ("Dow, S&P 500, Nasdaq Futures Dip As Bond Yields Bite And Trump-Xi Meet Grabs Spotlight: Why META, MCD, "
         "GOOG, U Stocks Are Trending", "market_commentary"),
        ("MIT Explains What Happens When the Trillion-Dollar AI Bubble Bursts", "market_commentary"),
        ("Stock Market Today: Small Caps Lead Dip Amid Hot Economic Data; Micron Keeps Drop In Control",
         "market_commentary"),
        ("History of Meta Platforms: Company timeline, facts & milestones", "market_commentary"),
        ("Is an AMD Stock Split Coming Now That the Shares Have Topped $600?", "opinion_or_question"),
        ("McDonald’s Increased Its Dividend by Nearly 4%. How to Play MCD Stock Here.", "opinion_or_question"),
        ("AMD Is Up 187% This Year: Take Profits, or Buy More?", "price_move_only"),
        ("Chip Trillionaires Club: Only One Clear Buy Among NVDA, AVGO, MU, AMD", "stock_picking_advice"),
        ("Sandisk vs. NVIDIA: Which AI Stock Is the Better Buy Now?", "stock_picking_advice"),
    ],
)
def test_non_events_are_filtered(headline, reason):
    verdict = non_event_reason(headline)
    assert verdict is not None and verdict.reason == reason, verdict


@pytest.mark.parametrize(
    "headline",
    [
        "Abbott to acquire Hologic for $15 billion in cash",
        "NVIDIA beats estimates and raises full-year revenue guidance",
        "Stifel Upgrades Microsoft to Buy, Raises Price Target to $575",
        "Nvidia beats estimates; Nasdaq futures rise",
        "Meta Platforms Unveils Muse AI Agent, Expands Smart Glasses and VR Push",
        "Palantir awarded $480 million Army contract",
        "FDA approves Madrigal liver drug for broader patient group",
        "Moderna shares slip after FDA issues complete response letter for flu vaccine",
    ],
)
def test_real_events_are_kept(headline):
    assert non_event_reason(headline) is None


# ----------------------------------------------------------------------------- third live run (FinBERT)


@pytest.mark.parametrize("headline", [
    "NN, Inc.: Repeated Guidance Raises Are Hard To Ignore (Ratings Upgrade)",
    "AMD: Welcome To The $1 Trillion Club; $740 Could Be Next (Rating Upgrade)",
])
def test_contributor_rating_labels_are_opinion_not_broker_actions(headline):
    from catalystedge.pipeline.noise import non_event_reason

    verdict = non_event_reason(headline)
    assert verdict is not None and verdict.reason == "stock_picking_advice"


@pytest.mark.parametrize(("headline", "symbol", "strength"), [
    ("Stifel Upgrades Microsoft to Buy, Raises Price Target to $575", "MSFT", "normal"),
    ("Microsoft Stock Wins Upgrade. Why This Analyst Sees 15% Upside.", "MSFT", "normal"),
    ("Keybanc Maintains Overweight on Meta Platforms, Raises Price Target to $900", "META", "weak"),
    ("Oppenheimer Maintains Outperform on Microsoft, Raises Price Target to $570", "MSFT", "weak"),
    ("Meta Stock Scores Price Target Hike. Analyst Says Muse 'Flips The Narrative' On AI Concerns.", "META", "weak"),
])
def test_price_target_raise_is_a_weak_upgrade(headline, symbol, strength):
    from catalystedge.pipeline.classify import classify
    from catalystedge.pipeline.ticker_link import Mention

    mention = Mention(symbol, "name", 0.9, headline.find(headline.split()[0]), 1, is_primary=True)
    events = {e.symbol: e for e in classify(headline, [mention], None)}
    assert events[symbol].event_type == "upgrade" and events[symbol].strength == strength


# ----------------------------------------------------------------------------- 4. contract wins

@pytest.mark.parametrize(
    ("headline", "symbol"),
    [
        ("Tesla wins lead role in 2,500-truck electric Class 8 order", "TSLA"),
        ("Palantir secures five-year $480 million contract with the U.S. Army", "PLTR"),
        ("Nvidia receives $2 billion order from Saudi data center operator", "NVDA"),
        ("Tesla books record 10,000-unit Semi order from PepsiCo", "TSLA"),
    ],
)
def test_contract_wins_are_recognised(headline, symbol):
    e = events(headline)[symbol]
    assert e.event_type == "contract_win" and e.is_signal_eligible, e.reasons


@pytest.mark.parametrize(
    "headline",
    [
        "Tesla wins over skeptics as investors cheer the robotaxi deal rumors",
        "Nvidia wins the week as chip stocks rally",
        "Tesla receives subpoena over order backlog disclosures",
    ],
)
def test_contract_rule_does_not_overreach(headline):
    assert all(e.event_type != "contract_win" or not e.is_signal_eligible for e in events(headline).values())



# ----------------------------------------------------------------------------- 5. press-release wording (8-K EX-99)

@pytest.mark.parametrize(
    ("headline", "event_type"),
    [
        ("Adobe Raises FY26 Total Revenue and Non-GAAP EPS Targets", "guidance_raise"),
        ("Adobe Raises FY25 Digital Media ARR Growth, Total Revenue, and EPS Targets", "guidance_raise"),
        ("Adobe Reports Record Q4 and FY2025 Revenue", "earnings_beat"),
        ("June quarter records for total company revenue and EPS", "earnings_beat"),
        ("All-time records for total company revenue and EPS", "earnings_beat"),
        ("Services revenue reaches new all-time high", "earnings_beat"),
    ],
)
def test_press_release_positive_wording(headline, event_type):
    from catalystedge.pipeline.ticker_link import Mention

    [e] = classify(headline, [Mention("ADBE", "filing", 0.99, is_primary=True)], None)
    assert (e.event_type, e.polarity) == (event_type, "positive"), e.reasons


@pytest.mark.parametrize(
    "headline",
    [
        "AMD Reports Second Quarter 2026 Financial Results",
        "Company posts record loss as revenue falls",
        "Stock hits record high after the announcement",
        "Adobe lowers FY26 revenue targets",
    ],
)
def test_press_release_neutral_or_negative_wording(headline):
    from catalystedge.pipeline.ticker_link import Mention

    [e] = classify(headline, [Mention("ADBE", "filing", 0.99, is_primary=True)], None)
    assert not e.is_signal_eligible, e.reasons


# ----------------------------------------------------------------------------- law-firm solicitations (live 2026-09-26)

LAW_FIRM_ADS = [
    "HDFC Bank Limited Securities Fraud Class Action Result of Deceptive Practices: Investors Have Opportunity "
    "to Lead",
    "AEVEX Corp. Notice of October 20, 2026 Application Deadline for Class Action Lawsuit",
    "Geron Investigation Initiated: Kahn Swick & Foti, LLC Investigates the Officers of Geron Corporation",
    "Rosen Law Firm Encourages Putnam Investment Management, LLC Mutual Fund Investors to Inquire About a Class",
    "HONA Stockholder Notice: Shareholder Rights Law Firm Robbins LLP Reminds Investors of the Lead Plaintiff "
    "Deadline",
    "Did Geron Corporation Insiders Breach their Fiduciary Duties to Shareholders?",
    "Alibaba Deadline: BABA Investors Have Opportunity to Lead Alibaba Group Holding Limited Securities Lawsuit",
    "BellRing Investor News: Rosen Law Firm Announces Investigation of Breaches of Fiduciary Duty",
]
REAL_LEGAL_NEWS = [
    "Tesla agrees to settle shareholder class action for $50 million",
    "Court dismisses securities class action against Acme Robotics",
    "Acme Robotics wins patent lawsuit against rival",
]


@pytest.mark.parametrize("headline", LAW_FIRM_ADS)
def test_law_firm_solicitations_are_not_events(headline):
    from catalystedge.pipeline.noise import non_event_reason

    v = non_event_reason(headline)
    assert v is not None and v.reason == "law_firm_solicitation"


@pytest.mark.parametrize("headline", REAL_LEGAL_NEWS)
def test_real_legal_news_is_kept(headline):
    from catalystedge.pipeline.noise import non_event_reason

    v = non_event_reason(headline)
    assert v is None or v.reason != "law_firm_solicitation"
