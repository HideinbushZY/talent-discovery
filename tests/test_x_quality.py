"""X 渠道 B（前沿声音）：查询过滤 + 营销号 bio 拦截 + 中文/中国导向门控。"""
from app.connectors.x import _build_query, _looks_spam
from app.pipeline import _china_oriented


def test_china_gate():
    # 中文问题 / 中国优先开关 → 门控掉 X
    assert _china_oriented("我们在做一款 AI 耳机，需要懂语音意图识别的人", china_first=False)
    assert _china_oriented("any english problem", china_first=True)
    # 全球英文问题 → 不门控，X 照跑
    assert not _china_oriented("We need a growth marketer for our DTC brand", china_first=False)


def test_query_phrases_first_and_filters():
    q = _build_query(["intent", "SLU"], ["语音意图识别"])
    assert q.startswith('("语音意图识别" OR intent OR SLU)')   # 精确短语在前
    for f in ("-is:retweet", "-is:reply", "-giveaway", "-hiring"):
        assert f in q


def test_query_empty():
    assert _build_query([], []) == ""


def test_spam_bio_blocked():
    assert _looks_spam("Make money with AI 👉 link in bio")
    assert _looks_spam("🎨 Web3 Content & Visuals | Contributor")
    assert _looks_spam("crypto trader | $BTC maxi")


def test_real_voice_bio_not_blocked():
    assert not _looks_spam("Founder & CEO @Soniox_ai — building voice AI")
    assert not _looks_spam("Speech recognition researcher, ex-DAMO")
    assert not _looks_spam("")


def test_blocklist_is_category_aware():
    # 技术难题：营销/赚钱/web3内容 算噪音
    assert _looks_spam("I help brands make money with content", category="technical")
    assert _looks_spam("🎨 Web3 Content & Visuals", category="technical")
    # 营销难题：同样的人正是目标，不该被误杀
    assert not _looks_spam("I help brands make money with content", category="marketing")
    assert not _looks_spam("🎨 Web3 Content & Visuals | Brand strategist", category="marketing")
    # 但诈骗/币圈 bot 在任何类别都丢
    assert _looks_spam("airdrop giveaway 🚀 next 100x memecoin", category="marketing")
