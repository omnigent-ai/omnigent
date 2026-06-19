"""E2e compaction test for the sessions-native path.

Migrated to use the mock LLM server. Uses pexpect to run multiple
turns within a single ``omnigent run`` session. With
``AP_CONTEXT_WINDOW_OVERRIDE=64`` the compaction budget (``0.8 * 64``
≈ 51 tokens) is tiny, so proactive compaction fires after the first
verbose mock turn.

The mock server is configured with long verbose responses so that the
persisted history grows past the tiny budget, triggering compaction,
while remaining deterministic.

Boot/turn synchronization goes through the shared ``_pexpect_harness``
helpers (the same path every green REPL e2e test uses).

``OMNIGENT_DATA_DIR`` isolates the runtime data dir so the test can
inspect the persisted compaction item without touching the developer's
``~/.omnigent``.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from tests.e2e.omnigent._pexpect_harness import (
    await_turn_complete,
    clean_exit,
    spawn_omnigent_run,
    submit_prompt,
    wait_for_ready,
)
from tests.e2e.omnigent.conftest import configure_mock_llm

_COMPACTION_AGENT_YAML = """\
name: compaction-e2e-test
description: Agent for e2e compaction testing.

executor:
  harness: openai-agents

prompt: |
  You are a test assistant. Reply with detailed, verbose answers
  so that conversation history grows quickly.
"""

_MODEL = "mock-compaction-e2e"
_HARNESS = "openai-agents"
_BOOT_TIMEOUT = 120.0
_RUNNING_TIMEOUT = 30.0
_TURN_TIMEOUT = 300.0
_EXIT_TIMEOUT = 20.0

# Visible turn-synchronization markers.
_RUNNING_MARKER = r"working"
_COMPLETION_MARKER = r"❯ "

# Mock responses: two verbose replies to trigger compaction, then
# one context-check reply that references the original request.
_TURN1_RESPONSE = (
    "Here are 20 countries with detailed information: "
    "1. France - Capital: Paris, Population: 68M, Language: French, "
    "Currency: Euro, Landmark: Eiffel Tower — an iconic iron lattice tower "
    "on the Champ de Mars in Paris, built 1887-1889. It was initially "
    "criticised by French artists but has become a global cultural icon. "
    "2. Germany - Capital: Berlin, Population: 84M, Language: German, "
    "Currency: Euro, Landmark: Brandenburg Gate — an 18th-century neoclassical "
    "monument. It was built on the orders of Prussian king Frederick William II. "
    "Now a symbol of unity and peace. "
    "3. Japan - Capital: Tokyo, Population: 125M, Language: Japanese, "
    "Currency: Yen, Landmark: Mount Fuji — an active stratovolcano and the "
    "highest peak in Japan at 3776m. A sacred site and artist's inspiration. "
    "4. Brazil - Capital: Brasilia, Population: 215M, Language: Portuguese, "
    "Currency: Real, Landmark: Christ the Redeemer — an Art Deco statue of "
    "Jesus Christ in Rio de Janeiro, 38m tall atop Corcovado mountain. "
    "5. Canada - Capital: Ottawa, Population: 38M, Language: English/French, "
    "Currency: CAD, Landmark: Niagara Falls — powerful waterfalls on the "
    "Niagara River bordering Canada and the US. Draws millions of visitors. "
    "6. Australia - Capital: Canberra, Population: 26M, Language: English, "
    "Currency: AUD, Landmark: Sydney Opera House — expressionist multi-venue "
    "performing arts centre with distinctive shell rooftops. UNESCO site. "
    "7. India - Capital: New Delhi, Population: 1.4B, Language: Hindi, "
    "Currency: Rupee, Landmark: Taj Mahal — white marble mausoleum in Agra "
    "built by Mughal emperor Shah Jahan. UNESCO World Heritage Site. "
    "8. China - Capital: Beijing, Population: 1.4B, Language: Mandarin, "
    "Currency: Yuan, Landmark: Great Wall — series of fortifications across "
    "northern China stretching over 21000km. Built over many centuries. "
    "9. Mexico - Capital: Mexico City, Population: 130M, Language: Spanish, "
    "Currency: Peso, Landmark: Chichen Itza — large pre-Columbian Mayan city "
    "in Yucatan Peninsula. One of the New Seven Wonders of the World. "
    "10. Argentina - Capital: Buenos Aires, Population: 46M, Language: Spanish, "
    "Currency: ARS, Landmark: Iguazu Falls — waterfalls of the Iguazu River "
    "on the Argentina-Brazil border. Wider than Victoria Falls. "
    "11. South Africa - Capital: Pretoria, Population: 60M, Language: Zulu, "
    "Currency: Rand, Landmark: Table Mountain — flat-topped mountain forming "
    "a prominent landmark overlooking Cape Town. Cable car access available. "
    "12. Egypt - Capital: Cairo, Population: 104M, Language: Arabic, "
    "Currency: EGP, Landmark: Great Pyramid of Giza — oldest of the Seven "
    "Wonders of the Ancient World, built as tomb for Pharaoh Khufu. "
    "13. Italy - Capital: Rome, Population: 60M, Language: Italian, "
    "Currency: Euro, Landmark: Colosseum — oval amphitheatre in centre of "
    "Rome, built 70-80 AD. Largest ancient amphitheatre ever built. "
    "14. Spain - Capital: Madrid, Population: 47M, Language: Spanish, "
    "Currency: Euro, Landmark: Sagrada Familia — large Roman Catholic church "
    "in Barcelona designed by Gaudi. Under construction since 1882. "
    "15. UK - Capital: London, Population: 67M, Language: English, "
    "Currency: GBP, Landmark: Big Ben — the nickname for the Great Bell of "
    "the striking clock at the Palace of Westminster. "
    "16. USA - Capital: Washington DC, Population: 331M, Language: English, "
    "Currency: USD, Landmark: Statue of Liberty — colossal neoclassical "
    "sculpture on Liberty Island in New York Harbor. Gift from France 1886. "
    "17. Russia - Capital: Moscow, Population: 144M, Language: Russian, "
    "Currency: Ruble, Landmark: Saint Basil Cathedral — cathedral on Red "
    "Square built 1555-1561. Features nine distinct chapels. "
    "18. Turkey - Capital: Ankara, Population: 85M, Language: Turkish, "
    "Currency: Lira, Landmark: Hagia Sophia — great mosque and formerly "
    "a church and a museum in Istanbul. Byzantine architecture. "
    "19. Greece - Capital: Athens, Population: 11M, Language: Greek, "
    "Currency: Euro, Landmark: Parthenon — former temple on Athenian Acropolis "
    "dedicated to goddess Athena. Construction began 447 BC. "
    "20. Peru - Capital: Lima, Population: 33M, Language: Spanish, "
    "Currency: Sol, Landmark: Machu Picchu — 15th-century Inca citadel in "
    "Andes Mountains at 2430m above sea level. UNESCO World Heritage Site."
)

_TURN2_RESPONSE = (
    "Here are 20 more countries not in the previous list: "
    "21. Thailand - Capital: Bangkok, Population: 70M, Language: Thai, "
    "Currency: Baht, Landmark: Grand Palace — an iconic complex of buildings "
    "in Bangkok that has been the official residence of the Kings of Siam. "
    "Construction began in 1782. "
    "22. South Korea - Capital: Seoul, Population: 52M, Language: Korean, "
    "Currency: Won, Landmark: Gyeongbokgung Palace — the largest of the Five "
    "Grand Palaces built by the Joseon dynasty. "
    "23. Netherlands - Capital: Amsterdam, Population: 17M, Language: Dutch, "
    "Currency: Euro, Landmark: Anne Frank House — a historic house and "
    "biographical museum dedicated to Anne Frank. "
    "24. Portugal - Capital: Lisbon, Population: 10M, Language: Portuguese, "
    "Currency: Euro, Landmark: Belem Tower — a fortified tower located in "
    "Lisbon built in the early 16th century. "
    "25. Sweden - Capital: Stockholm, Population: 10M, Language: Swedish, "
    "Currency: SEK, Landmark: Vasa Museum — a maritime museum displaying "
    "the 17th century warship Vasa. "
    "26. Norway - Capital: Oslo, Population: 5M, Language: Norwegian, "
    "Currency: NOK, Landmark: Geirangerfjord — a fjord in Stranda "
    "Municipality. UNESCO World Heritage Site. "
    "27. Denmark - Capital: Copenhagen, Population: 6M, Language: Danish, "
    "Currency: DKK, Landmark: The Little Mermaid — a bronze statue by "
    "Edvard Eriksen on a rock by the Copenhagen harbour. "
    "28. Switzerland - Capital: Bern, Population: 9M, Language: German/French, "
    "Currency: CHF, Landmark: Matterhorn — a large, near-symmetric pyramidal "
    "peak in the Alps. "
    "29. Austria - Capital: Vienna, Population: 9M, Language: German, "
    "Currency: Euro, Landmark: Schonbrunn Palace — a former imperial summer "
    "residence in Vienna. UNESCO World Heritage Site. "
    "30. Belgium - Capital: Brussels, Population: 12M, Language: French/Dutch, "
    "Currency: Euro, Landmark: Atomium — a building in Brussels originally "
    "built for Expo 58 World Fair. "
    "31. Poland - Capital: Warsaw, Population: 38M, Language: Polish, "
    "Currency: PLN, Landmark: Wawel Castle — a castle residency at the left "
    "bank of the Vistula river in Krakow. "
    "32. Czech Republic - Capital: Prague, Population: 11M, Language: Czech, "
    "Currency: CZK, Landmark: Charles Bridge — a medieval stone arch bridge "
    "that crosses the Vltava river in Prague. "
    "33. Hungary - Capital: Budapest, Population: 10M, Language: Hungarian, "
    "Currency: HUF, Landmark: Hungarian Parliament — the seat of the National "
    "Assembly of Hungary, the largest building in Hungary. "
    "34. Romania - Capital: Bucharest, Population: 19M, Language: Romanian, "
    "Currency: RON, Landmark: Bran Castle — a national monument and landmark "
    "associated with the legend of Dracula. "
    "35. Ukraine - Capital: Kyiv, Population: 44M, Language: Ukrainian, "
    "Currency: UAH, Landmark: Kyiv Pechersk Lavra — a historic Orthodox "
    "Christian monastery. UNESCO World Heritage Site. "
    "36. Nigeria - Capital: Abuja, Population: 220M, Language: English, "
    "Currency: NGN, Landmark: Olumo Rock — a mountain in Abeokuta, Ogun State "
    "used as a natural fortress by the Egba people. "
    "37. Kenya - Capital: Nairobi, Population: 55M, Language: Swahili, "
    "Currency: KES, Landmark: Masai Mara — a national reserve in Narok "
    "County known for exceptional wildlife and Great Migration. "
    "38. Morocco - Capital: Rabat, Population: 37M, Language: Arabic, "
    "Currency: MAD, Landmark: Djemaa el-Fna — a square and market place in "
    "Marrakesh Medina quarter. UNESCO intangible heritage site. "
    "39. Indonesia - Capital: Jakarta, Population: 274M, Language: Indonesian, "
    "Currency: IDR, Landmark: Borobudur — a 9th-century Mahayana Buddhist "
    "temple in Magelang Regency. UNESCO World Heritage Site. "
    "40. Pakistan - Capital: Islamabad, Population: 225M, Language: Urdu, "
    "Currency: PKR, Landmark: Badshahi Mosque — an iconic Mughal-era mosque "
    "in Lahore built in 1673 by Emperor Aurangzeb."
)

_TURN3_RESPONSE = (
    "You first asked me to list exactly 20 countries with detailed information "
    "about each one including capital city, population, official language, currency, "
    "and a famous landmark with a 3-sentence description, numbered 1 through 20."
)


def test_compaction_fires_and_agent_retains_context(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    Multi-turn mock test: 2 verbose turns trigger proactive
    compaction, then a 3rd turn proves the agent retains context.

    Uses the mock LLM server with pre-configured verbose responses
    so the tiny token budget is exceeded deterministically.

    Breakage this catches: if proactive compaction doesn't fire,
    the compaction item won't appear in the DB. If the summary
    doesn't capture prior context, turn 3 can't reference it.

    :param omnigent_python: Interpreter with omnigent installed.
    :param omnigent_repo_root: Working directory for the subprocess.
    :param mock_credentials_env: Mock-LLM env vars.
    :param mock_llm_server_url: Mock server URL.
    :param tmp_path: Per-test temp directory.
    """
    yaml_path = tmp_path / "compaction-e2e-test.yaml"
    yaml_path.write_text(_COMPACTION_AGENT_YAML)
    # Isolated runtime data dir: chat.db and the per-test local server's
    # pidfile both resolve under here so the test inspects its own DB.
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Configure three mock responses up front: two verbose answers
    # to trigger compaction, plus one context-check answer.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": _TURN1_RESPONSE},
            {"text": _TURN2_RESPONSE},
            {"text": _TURN3_RESPONSE},
        ],
        key=_MODEL,
    )

    env = dict(mock_credentials_env)
    # Tiny context window so the compaction budget (0.8 * window ≈ 51
    # tokens) is exceeded by the very first turn's history.
    env["AP_CONTEXT_WINDOW_OVERRIDE"] = "64"
    env["OMNIGENT_DATA_DIR"] = str(data_dir)

    child = spawn_omnigent_run(
        omnigent_python=omnigent_python,
        yaml_path=yaml_path,
        model=_MODEL,
        harness=_HARNESS,
        env=env,
        cwd=omnigent_repo_root,
        timeout=_TURN_TIMEOUT,
        no_log=True,
        # Keep sessions on: the test asserts on the persisted chat.db,
        # which only the sessions path writes.
        no_session=False,
    )
    try:
        wait_for_ready(child, timeout=_BOOT_TIMEOUT)

        submit_prompt(
            child,
            (
                "List exactly 20 countries. For each country, write the capital city, "
                "the population, the official language, the currency, and a famous "
                "landmark with a 3-sentence description. Number them 1 through 20."
            ),
        )
        turn1 = await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_TURN_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )
        assert len(turn1.stripped) > 100, f"Turn 1 too short: {turn1.stripped[:100]!r}"

        submit_prompt(
            child,
            (
                "Now list 20 MORE countries not in the previous list, same detailed "
                "format with capital, population, language, currency, and landmark."
            ),
        )
        turn2 = await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_TURN_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )
        assert len(turn2.stripped) > 100, f"Turn 2 too short: {turn2.stripped[:100]!r}"

        submit_prompt(
            child,
            "What was the very first thing I asked you? Reply in one sentence.",
        )
        turn3 = await_turn_complete(
            child,
            running_timeout=_RUNNING_TIMEOUT,
            completion_timeout=_TURN_TIMEOUT,
            running_marker=_RUNNING_MARKER,
            completion_pattern=_COMPLETION_MARKER,
        )

        # Wait for the server's relay to persist items before exit.
        time.sleep(5)
        clean_exit(child, timeout=_EXIT_TIMEOUT)
    finally:
        if not child.closed:
            child.close(force=True)

    # Verify compaction item was persisted to the DB.
    db_path = data_dir / "chat.db"
    assert db_path.is_file(), f"DB not found at {db_path}"
    with sqlite3.connect(str(db_path)) as conn:
        compaction_rows = conn.execute(
            "SELECT type FROM conversation_items WHERE type = 'compaction'"
        ).fetchall()
    # At least 1 compaction item: proactive compaction fired after a
    # verbose turn's history exceeded the tiny token budget.
    assert len(compaction_rows) >= 1, (
        f"Expected >= 1 compaction item in DB. Found {len(compaction_rows)}."
    )

    # Verify turn 3 references prior context — proves the compacted
    # summary preserved meaningful context.
    combined = turn3.stripped.lower()
    assert any(
        kw in combined for kw in ["countr", "capital", "landmark", "list", "nation", "asked"]
    ), f"Turn 3 doesn't reference prior context. Response: {turn3.stripped[:300]!r}"
