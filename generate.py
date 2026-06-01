import os
import re
import html
import time
import json
import logging
import socket
from datetime import datetime
import urllib.request
import xml.etree.ElementTree as ET
from pydantic import BaseModel, Field
from typing import Literal
from google import genai
from google.genai import types

# ネットワークフリーズ防止グローバルタイムアウト
socket.setdefaulttimeout(30)

# ==========================================
# 1. ログ・フォルダ初期設定
# ==========================================
os.makedirs("logs", exist_ok=True)
os.makedirs("articles", exist_ok=True)
os.makedirs("data", exist_ok=True)
os.makedirs("books", exist_ok=True)

logging.basicConfig(
    filename="logs/app.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

MAX_ARTICLES_LIMIT = 50
MAX_HISTORY_LIMIT = 5000
TEMPLATE_VERSION = "5.2.0"  # 5号店 生活防衛OS・テーマ自動分類 ＆ 検索リンク完全統合仕様

# ==========================================
# 2. Pydanticスキーマ定義（生活防衛OS・v4.2.0）
# ==========================================
class PersonaBenefit(BaseModel):
    persona_name: str = Field(description="この生活防衛・稼ぎ方に直結するターゲット。15文字以内。")
    benefit: str = Field(description="その立場の読者にとって、この知識が今日からどう役に立ち、どう生活が楽になるか。150〜200文字程度。")

class FAQItem(BaseModel):
    question: str = Field(description="想定される読者からのよくある質問。25文字以内。")
    answer: str = Field(description="質問に対する客観的で簡潔な回答。70文字以内。")

class ArticleOutputSchema(BaseModel):
    title: str = Field(description="不安・欲望・優越を刺激し、読者に「安心」を約束する35文字以内のタイトル。記事タイプに最適なSEOキーワードを必ず含めること。")
    search_intent: Literal['informational', 'commercial', 'transactional', 'navigational'] = Field(description="読者の検索意図を4分類から最適判定。")
    action_level: Literal['今すぐ申請', '今月中に確認', '知識として保存'] = Field(description="読者が今すぐ取るべき具体的なアクション指標。")
    life_stage: Literal['student', 'worker', 'family', 'senior', 'disabled'] = Field(description="この情報が最も深く突き刺さる読者の現在のライフステージ・属性。")
    
    # 6大テーマピラー統合スラグ
    pillar_slug: Literal['life-defense', 'career', 'side-business', 'household-optimization', 'asset-building', 'life-recovery'] = Field(description="この情報が属する大カテゴリのピラースラグ。")
    category: str = Field(description="大カテゴリ。例：『生活防衛』『攻めの副業』『人生再建』など。10文字以内。")
    topic_cluster: str = Field(description="親テーマ（クラスター）名。例：『障害年金サポート』など。15文字以内。")
    cluster_slug: str = Field(description="親テーマのスラグ。英数字ハイフンのみ。例:『disability-pension-guide』。")
    
    difficulty_level: Literal['beginner', 'intermediate', 'advanced', 'expert'] = Field(description="難易度。専門家レベルの内容はexpertを選択。")
    estimated_read_time: int = Field(description="想定読了時間（分）。3、5、8などの半角数値。")
    article_type: Literal['definition', 'comparison', 'application', 'troubleshooting', 'monetization'] = Field(description="記事のタイプ。")
    
    # 信頼性と緊急度の可視化（E-E-A-T）
    urgency_level: Literal['★★★☆☆', '★★★★☆', '★★★★★'] = Field(description="制度利用や対策の緊急度。")
    trust_score: Literal['★★★★☆', '★★★★★'] = Field(description="情報の信頼度スコア。公的機関ソースの場合は星5つを設定。")
    last_verified_date: str = Field(description="情報の最終確認日（例：'2026-06-01'）。")
    source_name: str = Field(description="情報元となった公式の一次情報源名。25文字以内。")
    source_url: str = Field(description="情報元の具体的な公的公式ページのURL。")
    
    book_tags: list[str] = Field(description="電子書籍のジャンル分類に使用するタグ。必ず2〜3つのタグを設定。例: ['生活防衛', '障害年金', '医療費削減']")
    
    # 30秒結論
    quick_definition: str = Field(description="この記事の結論や対策は一言で何か？ 体言止めで45文字以内。")
    quick_target: str = Field(description="どのような人向けのものか？ 25文字以内。")
    quick_features: list[str] = Field(description="主な特徴や要件。必ず3つの短いリスト。")
    quick_importance: str = Field(description="この記事の重要度判定。15文字以内。")
    
    # 概念理解のグラデーション
    one_word_summary: str = Field(description="一言でいうと（20文字以内）。")
    explain_level_1: str = Field(description="5歳児比喩。制度の論理を絶対に歪めず、100%日常の例えに変換して体言止め45文字以内。")
    explain_level_2: str = Field(description="簡単にいうと？（中学生向け）。200〜300文字程度。")
    explain_level_3: str = Field(description="つまりどういうこと？（社会人向け・生活上の実践的意義）。300〜450文字程度。")
    
    persona_benefits: list[PersonaBenefit] = Field(description="関連ターゲットとそのメリット。2〜3つ自律生成。")
    faq_list: list[FAQItem] = Field(description="想定されるFAQ。必ず3つ生成。")
    charo_insight: str = Field(description="編集長cocoroの眼。200文字程度. ")
    today_mission: str = Field(description="明日から読者が起こすべき具体的アクション。100文字程度。")
    slug: str = Field(description="半角英数字とハイフンのみのスラグ。")

# ==========================================
# 3. 各種ユーティリティ
# ==========================================
def sanitize_slug(raw_slug: str) -> str:
    slug = re.sub(r'[^a-z0-9\-]', '', raw_slug.lower())
    slug = re.sub(r'-+', '-', slug).strip('-')
    if not slug:
        slug = f"explain-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    return slug[:80]

# 一次情報リンクのハルシネーション完全ガード関数
def get_strategy_info(pillar_slug_candidate: str, article_text: str) -> dict:
    strategy_path = os.path.join("data", "strategy_master.json")
    default_info = {
        "source_name": "厚生労働省：公的支援・制度案内公式（Google検索）",
        "source_url": "https://www.google.com/search?q=厚生労働省+公的支援+制度+公式"
    }
    
    if not os.path.exists(strategy_path):
        return default_info
    try:
        with open(strategy_path, "r", encoding="utf-8") as f:
            strategy_data = json.load(f)
        
        pillar_data = strategy_data.get(pillar_slug_candidate)
        if not pillar_data:
            for key, val in strategy_data.items():
                trigger = val.get("keyword_trigger", "").lower()
                if trigger and trigger in article_text.lower():
                    pillar_data = val
                    break
                    
        if pillar_data and pillar_data.get("trust_links"):
            link = pillar_data["trust_links"][0]
            return {
                "source_name": link["title"],
                "source_url": link["url"]
            }
    except Exception as e:
        logging.error(f"戦略マスターリンク抽出失敗: {e}")
    return default_info

def get_strategy_context(article_text: str) -> str:
    strategy_path = os.path.join("data", "strategy_master.json")
    if not os.path.exists(strategy_path):
        return ""
    try:
        with open(strategy_path, "r", encoding="utf-8") as f:
            strategy_data = json.load(f)
        
        matched_info = []
        text_lower = article_text.lower()
        
        for key, value in strategy_data.items():
            trigger = value.get("keyword_trigger", "").lower()
            if trigger and (trigger in text_lower or key.lower() in text_lower):
                keywords_str = ", ".join(value.get("seo_keywords", []))
                links_str = "\n".join([f"- [{l['title']}]({l['url']})" for l in value.get("trust_links", [])])
                matched_info.append(f"【生活防衛OS戦略カテゴリ: {key}】\n■ターゲットSEOキーワード: {keywords_str}\n■一次情報公式リンク:\n{links_str}")
        
        if matched_info:
            return "\n\n=== 突合された公的一次情報 ＆ 対策キーワード ===\n" + "\n\n".join(matched_info)
    except Exception as e:
        logging.error(f"戦略マスター突合失敗: {e}")
    return ""

HISTORY_FILE = "logs/history.json"
def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"履歴読み込み失敗: {e}")
    return []

def save_history(history: list):
    try:
        trimmed = history[-MAX_HISTORY_LIMIT:]
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(trimmed, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"履歴保存失敗: {e}")

# ==========================================
# 4. RSS取得・スクレイピング
# ==========================================
def fetch_rss_feed(rss_url: str) -> list:
    articles = []
    try:
        req = urllib.request.Request(
            rss_url,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            root = ET.fromstring(response.read())
        for item in root.findall('.//item'):
            title = item.find('title').text if item.find('title') is not None else ""
            link = item.find('link').text if item.find('link') is not None else ""
            desc = item.find('description').text if item.find('description') is not None else ""
            articles.append({"title": title, "link": link, "description": desc})
    except Exception as e:
        logging.error(f"RSS取得失敗: {e}")
    return articles

def fetch_full_article_text(url: str) -> str:
    try:
        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            html_content = response.read().decode('utf-8', errors='ignore')
        for tag in ['script', 'style', 'header', 'footer', 'nav']:
            html_content = re.sub(f'<{tag}[\\s\\S]*?>[\\s\\S]*?</{tag}>', '', html_content)
        html_content = re.sub(r'</?(p|div|h1|h2|h3|h4|li|br)[^>]*>', '\n', html_content)
        text = re.sub(r'<[^>]+>', ' ', html_content)
        text = html.unescape(text)
        return re.sub(r'\n\s*\n+', '\n', text).strip()
    except Exception as e:
        logging.warning(f"全文スクレイピング失敗: {e}")
        return ""

# ==========================================
# 5. レイアウト結合エンジン（ピラー優先ソート）
# ==========================================
def build_page(body_template_path, title, date_iso, date_ja, source_url, source_name, replacements, output_path, is_article=False, slug="", art=None, all_articles=None) -> bool:
    try:
        if not os.path.exists("layout.html") or not os.path.exists(body_template_path):
            logging.error(f"テンプレート欠損: {body_template_path}")
            return False

        with open("layout.html", "r", encoding="utf-8") as f:
            layout_content = f.read()
        with open(body_template_path, "r", encoding="utf-8") as f:
            body_content = f.read()

        combined_content = layout_content.replace("{{BODY_CONTENT}}", body_content)

        # 関連記事・ロードマップ（ピラー優先 ＆ 4段階難易度ソート）
        if is_article and art and all_articles:
            related_html = ""
            cluster_articles = []
            backup_articles = []

            for _, art_data in all_articles:
                if art_data["slug"] == slug:
                    continue
                if art_data.get("pillar_slug") == art.get("pillar_slug"):
                    cluster_articles.append(art_data)
                elif art_data.get("category") == art.get("category"):
                    backup_articles.append(art_data)

            # 4段階難易度ソートロジック
            curr_diff = art.get("difficulty_level", "beginner")
            if curr_diff == "intermediate":
                sort_order = ["intermediate", "advanced", "expert", "beginner"]
            elif curr_diff == "advanced":
                sort_order = ["advanced", "expert", "intermediate", "beginner"]
            elif curr_diff == "expert":
                sort_order = ["expert", "advanced", "intermediate", "beginner"]
            else:
                sort_order = ["beginner", "intermediate", "advanced", "expert"]

            cluster_articles.sort(key=lambda x: sort_order.index(x.get("difficulty_level", "beginner")) if x.get("difficulty_level", "beginner") in sort_order else 99)
            backup_articles.sort(key=lambda x: sort_order.index(x.get("difficulty_level", "beginner")) if x.get("difficulty_level", "beginner") in sort_order else 99)

            final_related = (cluster_articles + backup_articles)[:3]

            for r_art in final_related:
                diff_ja = {"beginner": "基本知識", "intermediate": "申請手順", "advanced": "応用手続", "expert": "専門要件"}.get(r_art.get("difficulty_level", "beginner"), "基本")
                related_html += f"""
                <article class="article-card fade-element">
                    <div class="article-meta">
                        <span class="difficulty-tag" style="border: 1px solid var(--border-color); padding: 1px 6px; border-radius: 4px; font-weight:700;">{diff_ja}</span>
                        <span>{html.escape(r_art.get('topic_cluster', '現代用語'))}</span>
                    </div>
                    <h3>{html.escape(r_art['title'])}</h3>
                    <p>{html.escape(r_art['one_word_summary'])}</p>
                    <a href="articles/{r_art['slug']}.html">解説を体系的に読む &rarr;</a>
                </article>
                """
            replacements["{{RELATED_ARTICLES_HTML}}"] = related_html

        # 置換の安全実行
        raw_keys = ["{{RELATED_ARTICLES_HTML}}", "{{WEEKLY_BOOK_BANNER}}", "{{ARTICLES_GRID}}", "{{BOOK_CONTENT}}", "{{PERSONA_BENEFITS_HTML}}", "{{FAQ_LIST_HTML}}", "{{INDEX_NAVIGATION_HTML}}"]
        for placeholder, value in replacements.items():
            if placeholder in raw_keys:
                combined_content = combined_content.replace(placeholder, value)
            else:
                combined_content = combined_content.replace(placeholder, html.escape(str(value)))

        # 構造化データとパスの動的切替
        if is_article:
            combined_content = combined_content.replace("{{CSS_PATH}}", "/style.css")
            combined_content = combined_content.replace("{{JS_PATH}}", "/script.js")
            
            # Combined JSON-LD
            ld_json_graph = {
                "@context": "https://schema.org",
                "@graph": [
                    {
                        "@type": "Article",
                        "@id": f"https://support.pray-power-is-god-and-cocoro.com/articles/{slug}.html#article",
                        "headline": title,
                        "datePublished": date_iso,
                        "author": {"@type": "Person", "name": "cocoro"},
                        "description": art.get("one_word_summary", title) if art else title,
                        "mainEntityOfPage": source_url
                    },
                    {
                        "@type": "BreadcrumbList",
                        "@id": f"https://support.pray-power-is-god-and-cocoro.com/articles/{slug}.html#breadcrumb",
                        "itemListElement": [
                            {"@type": "ListItem", "position": 1, "name": "Home", "item": "https://support.pray-power-is-god-and-cocoro.com/"},
                            {"@type": "ListItem", "position": 2, "name": art.get("category", "生活防衛") if art else "生活防衛", "item": "https://support.pray-power-is-god-and-cocoro.com/"},
                            {"@type": "ListItem", "position": 3, "name": art.get("topic_cluster", "クラスター") if art else "クラスター", "item": "https://support.pray-power-is-god-and-cocoro.com/"}
                        ]
                    }
                ]
            }
            if art and art.get("faq_list"):
                ld_json_graph["@graph"].append({
                    "@type": "FAQPage",
                    "@id": f"https://support.pray-power-is-god-and-cocoro.com/articles/{slug}.html#faq",
                    "mainEntity": [
                        {
                            "@type": "Question",
                            "name": item["question"],
                            "acceptedAnswer": {"@type": "Answer", "text": item["answer"]}
                        } for item in art["faq_list"]
                    ]
                })

            serialized_json = json.dumps(ld_json_graph, ensure_ascii=False, indent=2)
            combined_content = combined_content.replace("{{STRUCTURED_DATA}}", f'<script type="application/ld+json">\n{serialized_json}\n</script>')
        else:
            combined_content = combined_content.replace("{{CSS_PATH}}", "style.css")
            combined_content = combined_content.replace("{{JS_PATH}}", "script.js")
            structured_data = """
            <script type="application/ld+json">
            {
              "@context": "https://schema.org",
              "@type": "WebSite",
              "name": "AI Frontier Life Support",
              "url": "https://support.pray-power-is-god-and-cocoro.com/"
            }
            </script>
            """
            combined_content = combined_content.replace("{{STRUCTURED_DATA}}", structured_data)

        # 平文置換
        combined_content = combined_content.replace("{{TITLE}}", html.escape(title))
        combined_content = combined_content.replace("{{DATE_ISO}}", date_iso)
        combined_content = combined_content.replace("{{DATE_JA}}", date_ja)
        combined_content = combined_content.replace("{{SOURCE_URL}}", html.escape(source_url))
        combined_content = combined_content.replace("{{SOURCE_NAME}}", html.escape(source_name))

        if art:
            combined_content = combined_content.replace("{{CATEGORY}}", html.escape(art.get("category", "生活防衛")))
            combined_content = combined_content.replace("{{TOPIC_CLUSTER}}", html.escape(art.get("topic_cluster", "クラスター")))
            combined_content = combined_content.replace("{{ARTICLE_TYPE}}", html.escape(art.get("article_type", "definition").upper()))
            combined_content = combined_content.replace("{{DIFFICULTY_LEVEL}}", html.escape(art.get("difficulty_level", "beginner").upper()))
            combined_content = combined_content.replace("{{ESTIMATED_READ_TIME}}", html.escape(str(art.get("estimated_read_time", 3))))

        tmp_out = output_path + ".tmp"
        with open(tmp_out, "w", encoding="utf-8") as f:
            f.write(combined_content)
        os.replace(tmp_out, output_path)
        return True
    except Exception as e:
        logging.error(f"ビルド失敗 ({output_path}): {e}")
        return False

# ==========================================
# 6. コア：生活防衛OS記事のAI自動生成（比喩正確性＆リンク切れ防止）
# ==========================================
def run_article_generator(source_text: str, source_url: str, source_name: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        logging.error("GEMINI_API_KEY が設定されていません。")
        return ""

    safe_text = source_text[:12000]
    strategy_context = get_strategy_context(safe_text)

    client = genai.Client(api_key=api_key)
    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    prompt = f"""
    あなたは、激変する情報過多社会において、読者に「生活防衛・自立のための智慧としての生活防衛OS辞典」を授ける最高峰の福祉・金融専門の編集長です。
    提供された【情報素材】と【生存戦略マスター情報】をもとに、以下の【ルール】に沿って全自動執筆してください。

    【ルール】
    - 単なる辞書的解説ではなく、「この記事を通じて、経済的・精神的に困っている人の暮らしが今日から具体的にどうラクになるか」に焦点をあててください。
    - タイトルは35文字以内。突合された「最新」を狙うSEOキーワードを必ず1つ以上自然に含めること。
    - 【比喩正確性ガード】: explain_level_1（5歳児比喩）は、読者が一瞬で直感的にイメージを掴めるよう、日常の例え（お店、おもちゃ、ルールなど）に変換してください。ただし、行政制度の法的根拠や申請要件の論理を絶対に歪めない、厳密な正確性を死守すること。
    - explain_level_2（簡単にいうと？）は、専門用語を使わずに中学生が読んでも100%理解できるように200〜300文字程度で論理的に記述してください。
    - explain_level_3（つまりどういうこと？）は、現代のセーフティネットや生活上の実践的な意義と関連づけ、社会人向けに300〜450文字程度で詳細記述してください。
    - persona_benefitsには、異なるターゲット層（障害当事者、在宅ワークを考えている主婦、生活防衛したい会社員など）を2〜3つ自律生成してメリットを記述してください。
    - faq_listには、読者がその用語に関して抱く可能性の高い疑問を3つQ&A形式で正確に記述してください。
    - source_name, source_urlには【情報素材】内に登場する厚生労働省、全国健康保険協会、金融庁等の公式一次情報公式URLを優先的に見つけ出して設定してください。

    【情報素材】
    {safe_text}
    {strategy_context}
    """

    MAX_RETRIES = 3
    response_text = ""
    for attempt in range(MAX_RETRIES):
        try:
            logging.info(f"Gemini API 呼び出し中 (試行 {attempt + 1}/{MAX_RETRIES})...")
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ArticleOutputSchema,
                    http_options=types.HttpOptions(timeout=60000)
                )
            )
            if response and response.text:
                response_text = response.text.strip()
                break
            else:
                raise ValueError("空のレスポンスを受信しました。")
        except Exception as e:
            wait = 2 ** attempt
            logging.warning(f"API呼び出し一時失敗（試行 {attempt + 1}）: {e}。リトライします...")
            time.sleep(wait)
    else:
        logging.error("リトライ超過。生成を断念します。")
        return ""

    response_text = re.sub(r"^```json\s*|\s*```$", "", response_text, flags=re.IGNORECASE).strip()

    try:
        data = json.loads(response_text)
        validated = ArticleOutputSchema(**data)
    except Exception as e:
        logging.error(f"Pydantic検証失敗: {e}\nレスポンス: {response_text}")
        return ""

    art = validated.model_dump()
    
    # 🛡️ 【リンクハルシネーション完全ガード機構】
    # AIが生成したURLがズレるのを防ぐため、strategy_masterから確定的にGoogle検索リンクをマッピングします。
    p_slug = art.get("pillar_slug", "life-defense")
    correct_source = get_strategy_info(p_slug, safe_text)
    
    art["source_name"] = correct_source["source_name"]
    art["source_url"] = correct_source["source_url"]

    slug = sanitize_slug(art["slug"])

    # JSONデータを保存
    art["template_version"] = TEMPLATE_VERSION
    output_json_path = os.path.join("data", f"{slug}.json")
    
    try:
        with open(output_json_path, "w", encoding="utf-8") as f:
            json.dump(art, f, ensure_ascii=False, indent=2)
        logging.info(f"記事生成 ＆ JSONデータ保存成功: {slug}")
        return slug
    except Exception as e:
        logging.error(f"JSON保存失敗: {e}")
        return ""

# ==========================================
# 7. サイト内完結型プチ生存書籍パブリッシャー
# ==========================================
def get_weekly_book_banner_html() -> str:
    if not os.path.exists("books"):
        return ""
    book_files = [f for f in os.listdir("books") if f.endswith(".html")]
    if not book_files:
        return ""
    book_files.sort(key=lambda x: os.path.getmtime(os.path.join("books", x)), reverse=True)
    latest_book = book_files[0]
    book_slug = os.path.splitext(latest_book)[0]
    display_title = f"{datetime.now().strftime('%Y年%m月')} 最新号：現代社会を自立して生き抜くための、生活防衛統合ロードマップ"
    
    return f"""
    <section class="weekly-book-banner fade-element" style="margin-bottom: 40px;">
        <div style="background: linear-gradient(135deg, #0f172a, #1e293b); color: white; padding: 30px; border-radius: 16px; box-shadow: 0 8px 24px rgba(0,0,0,0.08); text-align: center;">
            <span style="background: rgba(255, 255, 255, 0.15); padding: 4px 12px; border-radius: 999px; font-size: 0.8rem; font-weight: 800; letter-spacing: 0.05em;">🆕 SURVIVAL WEEKLY BOOK 配信中</span>
            <h2 style="font-size: 1.6rem; font-weight: 800; margin: 15px 0 10px; color: white;">{display_title}</h2>
            <p style="font-size: 0.95rem; color: rgba(255, 255, 255, 0.85); max-width: 500px; margin: 0 auto 20px; line-height: 1.6;">蓄積された専門制度・お金の防衛策を体系的なロードマップとして再編集。自分の足で立ち、穏やかに生き抜くための特別書籍です。</p>
            <a href="books/{book_slug}.html" class="toggle-button" style="background: white; color: #1e293b; border: none; font-weight: 800; margin-top: 0; display: inline-block; padding: 12px 24px; border-radius: 8px; text-decoration: none;">電子書籍を読む（無料） &rarr;</a>
        </div>
    </section>
    """

def generate_weekly_book():
    logging.info("=== 自動週刊書籍パブリッシング開始 ===")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return

    try:
        json_files = [f for f in os.listdir("data") if f.endswith(".json") and f != "strategy_master.json"]
        if len(json_files) < 5:
            logging.info("記事数不足により書籍生成を保留します。")
            return

        combined_materials = []
        for j_file in json_files[:15]:
            try:
                with open(os.path.join("data", j_file), "r", encoding="utf-8") as f:
                    art = json.load(f)
                combined_materials.append(f"【生活防衛概念】: {art['title']}\n【本質】: {art['one_word_summary']}\n【解説】: {art['explain_level_3']}\n【ココロの眼】: {art['charo_insight']}")
            except Exception as e:
                continue

        if not combined_materials:
            return

        client = genai.Client(api_key=api_key)
        model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        materials_text = "\n\n---\n\n".join(combined_materials)

        prompt = f"""
        あなたは、激変するAI社会において、人々に温かく寄り添い、確かな生活防衛を支援する最高峰の編集責任者です。
        以下の【生活概念・データの断片】を美しく統合し、自立のための体系的電子書籍を執筆してください。

        【ルール】
        - markdownの装飾（```html や ``` など）はいっさい出力せず、直接 <h3>, <p>, <strong>, <blockquote> 等のHTMLタグだけを出力してください。
        - 各段落は、CSS側のマージン設定によって美しく配置されます。
        """

        book_html_content = ""
        for attempt in range(3):
            try:
                logging.info(f"Gemini API 書籍執筆中 (試行 {attempt + 1})...")
                response = client.models.generate_content(model=model_name, contents=prompt)
                if response and response.text:
                    book_html_content = response.text.strip()
                    break
                else:
                    raise ValueError("レスポンスが空です。")
            except Exception as e:
                time.sleep(2 ** attempt)
        else:
            return

        book_html_content = re.sub(r"^```html\s*|\s*```$", "", book_html_content, flags=re.IGNORECASE).strip()
        book_title = f"{datetime.now().strftime('%Y年%m月')}号：AI時代の生活防衛 ＆ お金と福祉の統合マスターバイブル"
        book_slug = f"weekly-survival-book-{datetime.now().strftime('%Y-%m-w%W')}"
        
        build_page(
            body_template_path="template_book.html",
            title=book_title,
            date_iso=datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00"),
            date_ja=datetime.now().strftime("%Y年%m月%d日"),
            source_url="#",
            source_name="AI Frontier Life Support 編集部",
            replacements={"{{BOOK_CONTENT}}": book_html_content},
            output_path=os.path.join("books", f"{book_slug}.html"),
            is_article=True,
            slug=book_slug
        )
    except Exception as e:
        logging.error(f"電子書籍生成エラー: {e}")

# ==========================================
# 8. 再ビルド（SSGテーマ自動分類・最新1件フルレンダリング）
# ==========================================
def rebuild_index_and_rotate_storage():
    try:
        json_files = [f for f in os.listdir("data") if f.endswith(".json") and f != "strategy_master.json"]
        all_articles = []

        for j_file in json_files:
            path = os.path.join("data", j_file)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    article_data = json.load(f)
                mtime = os.path.getmtime(path)
                all_articles.append((mtime, article_data))
            except Exception as e:
                logging.error(f"JSON読込失敗: {e}")

        all_articles.sort(key=lambda x: x[0], reverse=True)

        if len(all_articles) > MAX_ARTICLES_LIMIT:
            logging.info("古い記事のローテーション削除を実施します。")
            to_delete = all_articles[MAX_ARTICLES_LIMIT:]
            all_articles = all_articles[:MAX_ARTICLES_LIMIT]
            for _, d_art in to_delete:
                d_slug = sanitize_slug(d_art["slug"])
                for p in [os.path.join("articles", f"{d_slug}.html"), os.path.join("data", f"{d_slug}.json")]:
                    if os.path.exists(p):
                        os.remove(p)

        if not all_articles:
            logging.info("ビルド対象データがありません。")
            return

        # 1. すべての個別記事を再ビルド
        for mtime, art in all_articles:
            a_slug = sanitize_slug(art["slug"])
            a_date_ja = datetime.fromtimestamp(mtime).strftime("%Y年%m月%d日 %H:%M")
            a_date_iso = datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S+09:00")
            
            benefits_html = ""
            for p_ben in art.get("persona_benefits", []):
                benefits_html += f"""
                <div class="level-box" style="background: var(--card-bg); border: 1px solid var(--border-color); padding: 25px; border-radius: 16px; margin-bottom: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.01);">
                    <h4 style="font-size: 1.1rem; font-weight: 800; margin-top: 0; margin-bottom: 12px; color: var(--accent-color);">👤 {html.escape(p_ben['persona_name'])}にとっての価値</h4>
                    <p style="font-size: 1rem; line-height: 1.8; color: var(--text-color); margin: 0; text-align: justify;">{html.escape(p_ben['benefit'])}</p>
                </div>
                """
            
            faq_html = ""
            for faq_item in art.get("faq_list", []):
                faq_html += f"""
                <div style="margin-bottom: 20px; border: 1px solid var(--border-color); border-radius: 12px; padding: 18px; background: var(--bg-accent);">
                    <strong style="display: block; font-size: 1rem; color: var(--text-color); margin-bottom: 8px;">💡 Q. {html.escape(faq_item['question'])}</strong>
                    <span style="display: block; font-size: 0.95rem; line-height: 1.6; color: var(--text-muted);">A. {html.escape(faq_item['answer'])}</span>
                </div>
                """

            build_page(
                body_template_path="template_article.html",
                title=art["title"],
                date_iso=a_date_iso,
                date_ja=a_date_ja,
                source_url=art.get("source_url", "#"),
                source_name=art.get("source_name", "ソース"),
                replacements={
                    "{{QUICK_DEFINITION}}": art["quick_definition"],
                    "{{QUICK_TARGET}}": art["quick_target"],
                    "{{QUICK_IMPORTANCE}}": art["quick_importance"],
                    "{{ONE_WORD_SUMMARY}}": art["one_word_summary"],
                    "{{EXPLAIN_LEVEL_1}}": art["explain_level_1"],
                    "{{EXPLAIN_LEVEL_2}}": art["explain_level_2"],
                    "{{EXPLAIN_LEVEL_3}}": art["explain_level_3"],
                    "{{CHARO_INSIGHT}}": art["charo_insight"],
                    "{{TODAY_MISSION}}": art["today_mission"],
                    "{{SEARCH_INTENT}}": art.get("search_intent", "informational").upper(),
                    "{{ACTION_LEVEL}}": art.get("action_level", "知識として保存"),
                    "{{PERSONA_BENEFITS_HTML}}": benefits_html,
                    "{{FAQ_LIST_HTML}}": faq_html
                },
                output_path=os.path.join("articles", f"{a_slug}.html"),
                is_article=True,
                slug=a_slug,
                art=art,
                all_articles=all_articles
            )

        # 2. 6大テーマ（Pillar）ごとの自動分類マップの生成（完全自動・読み仮名不要）
        pillar_names = {
            "life-defense": "🛡️ 生活防衛（福祉・制度）",
            "career": "💼 仕事・キャリア",
            "side-business": "🚀 攻めの副業",
            "household-optimization": "📉 家計改善・節約",
            "asset-building": "📈 資産形成（NISA等）",
            "life-recovery": "🤝 人生再建・相談窓口"
        }

        index_map = {}
        for _, art in all_articles:
            p_slug = art.get("pillar_slug", "life-defense")
            if p_slug not in index_map:
                index_map[p_slug] = []
            index_map[p_slug].append(art)

        # アーカイブ用テーマナビゲーションHTMLの作成
        nav_elements = []
        for p_slug, p_name in pillar_names.items():
            if p_slug in index_map:
                nav_elements.append(f'<a href="#pillar-{p_slug}" style="font-weight: 800; text-decoration: underline; margin: 0 5px;">{p_name}</a>')
        nav_html = " | ".join(nav_elements)

        # 3. index.html ビルド（最新1件フルレンダリング ＆ 過去ログのグリッドカード一覧）
        _, hero_art = all_articles[0]
        hero_date_ja = datetime.now().strftime("%Y年%m月%d日 %H:%M")
        hero_date_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")

        articles_grid_html = ""
        for idx, (_, art) in enumerate(all_articles[1:]):
            a_title = html.escape(art["title"])
            a_slug = sanitize_slug(art["slug"])
            diff_ja = {"beginner": "基本知識", "intermediate": "申請手順", "advanced": "応用手続", "expert": "専門要件"}.get(art.get("difficulty_level", "beginner"), "基本")
            
            if idx == 3:
                articles_grid_html += """
                <div class="adsense-container" style="text-align: center; margin: 20px 0; min-height: 100px; width:100%;">
                    <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-2908004621823900" crossorigin="anonymous"></script>
                    <ins class="adsbygoogle"
                         style="display:block"
                         data-ad-format="fluid"
                         data-ad-layout-key="-fb+5w+4e-db+86"
                         data-ad-client="ca-pub-2908004621823900"
                         data-ad-slot="3799886389"></ins>
                    <script>(adsbygoogle = window.adsbygoogle || []).push({});</script>
                </div>
                """

            articles_grid_html += f"""
                <article class="article-card fade-element">
                    <div class="article-meta">
                        <span class="intent-badge">{html.escape(art.get('search_intent', 'informational').upper())}</span>
                        <span class="difficulty-tag" style="border: 1px solid var(--border-color); padding: 1px 6px; border-radius: 4px;">{diff_ja}</span>
                        <span class="action-badge" style="background: var(--tag-bg); padding: 1px 6px; border-radius: 4px;">{html.escape(art.get('action_level', '知識として保存'))}</span>
                    </div>
                    <h3>{a_title}</h3>
                    <p>{html.escape(art['one_word_summary'])}</p>
                    <a href="articles/{a_slug}.html">解説を読む &rarr;</a>
                </article>
            """

        weekly_book_banner = get_weekly_book_banner_html()

        build_page(
            body_template_path="template_index.html",
            title=hero_art["title"],
            date_iso=hero_date_iso,
            date_ja=hero_date_ja,
            source_url=hero_art.get("source_url", "#"),
            source_name=hero_art.get("source_name", "ソース"),
            replacements={
                "{{QUICK_DEFINITION}}": hero_art["quick_definition"],
                "{{ONE_WORD_SUMMARY}}": hero_art["one_word_summary"],
                "{{EXPLAIN_LEVEL_2}}": hero_art["explain_level_2"],
                "{{EXPLAIN_LEVEL_3}}": hero_art["explain_level_3"],
                "{{CHARO_INSIGHT}}": hero_art["charo_insight"],
                "{{TODAY_MISSION}}": hero_art["today_mission"],
                "{{SEARCH_INTENT}}": hero_art.get("search_intent", "informational").upper(),
                "{{ACTION_LEVEL}}": hero_art.get("action_level", "知識として保存"),
                "{{WEEKLY_BOOK_BANNER}}": weekly_book_banner,
                "{{ARTICLES_GRID}}": articles_grid_html
            },
            output_path="index.html",
            is_article=False
        )

        # 4. archive.htmlのビルド（テーマ別自動分類出力）
        archive_html = ""
        for p_slug, p_name in pillar_names.items():
            if p_slug in index_map:
                archive_html += f'<h3 id="pillar-{p_slug}" style="font-size: 1.4rem; margin-top: 40px; margin-bottom: 20px; border-bottom: 2px solid var(--accent-color); padding-bottom: 5px;">{p_name}</h3>'
                archive_html += '<div class="articles-grid">'
                for art in index_map[p_slug]:
                    a_title = html.escape(art["title"])
                    a_slug = sanitize_slug(art["slug"])
                    diff_ja = {"beginner": "基本知識", "intermediate": "申請手順", "advanced": "応用手続", "expert": "専門要件"}.get(art.get("difficulty_level", "beginner"), "基本")
                    archive_html += f"""
                        <article class="article-card fade-element">
                            <div class="article-meta">
                                <span class="difficulty-tag" style="border: 1px solid var(--border-color); padding: 1px 6px; border-radius: 4px;">{diff_ja}</span>
                                <span>{html.escape(art.get('topic_cluster', '生活防衛OS'))}</span>
                            </div>
                            <h3>{a_title}</h3>
                            <p>{html.escape(art['one_word_summary'])}</p>
                            <a href="articles/{a_slug}.html">解説を読む &rarr;</a>
                        </article>
                    """
                archive_html += '</div>'

        build_page(
            body_template_path="template_archive.html",
            title="現代生活防衛知識 検索・索引アーカイブ",
            date_iso=hero_date_iso,
            date_ja=hero_date_ja,
            source_url="#",
            source_name="アーカイブ",
            replacements={
                "{{INDEX_NAVIGATION_HTML}}": nav_html,
                "{{ARTICLES_GRID}}": archive_html
            },
            output_path="archive.html",
            is_article=False
        )
        print("✅ 5号店生活防衛OS：再ビルド・索引生成が正常に完了しました！")
    except Exception as e:
        logging.error(f"再ビルドエラー: {e}")

# ==========================================
# 9. オーケストレーター（メイン処理）
# ==========================================
def main():
    RSS_FEEDS = [
        {"url": "https://www.reutersagency.com/feed/?best-topics=tech&post_type=best", "name": "Reuters Tech Strategy"},
        {"url": "https://www.cnbc.com/id/19854910/device/rss/rss.html", "name": "CNBC Tech Life Strategy"}
    ]

    logging.info("--- 5号店：生活防衛OS自動クローリング開始 ---")
    history = load_history()
    processed_urls = {h["url"] for h in history if "url" in h}
    new_article_created = False
    
    data_files = [f for f in os.listdir("data") if f.endswith(".json") and f != "strategy_master.json"]
    
    if not data_files:
        if os.environ.get("ALLOW_DEMO_SEED", "true").lower() == "true":
            mock_text = "The Disability Pension (Shougai Nenkin) in Japan is a public social security system. Under the program, individuals with mental disorders, including depression or schizophrenia, can apply for grades 1, 2, or 3 financial assistance. Combined with the Services and Supports for Persons with Disabilities Act (Jiritsu Shien Iryou), medical expenses for outpatient psychiatric treatments can be reduced to 10 percent of the total."
            slug = run_article_generator(mock_text, "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/nenkin/nenkin/shougai.html", "厚生労働省 障害年金案内公式")
            if slug:
                new_article_created = True

    MAX_PROCESS_PER_RUN = 1
    processed_count = 0

    for feed in RSS_FEEDS:
        fetched = fetch_rss_feed(feed["url"])
        if not fetched:
            continue
        if processed_count >= MAX_PROCESS_PER_RUN:
            break

        for item in fetched:
            if processed_count >= MAX_PROCESS_PER_RUN:
                break
            if item["link"] in processed_urls:
                continue

            if not item["description"] or len(item["description"]) < 100:
                history.append({"url": item["link"], "processed_at": datetime.now().isoformat(), "status": "skipped"})
                processed_urls.add(item["link"])
                continue

            logging.info(f"未処理ニュース検知: {item['title']}")
            full_text = fetch_full_article_text(item["link"])
            if not full_text:
                full_text = item["description"]

            slug = run_article_generator(full_text, item["link"], feed["name"])
            if slug:
                new_article_created = True
                history.append({"url": item["link"], "processed_at": datetime.now().isoformat(), "status": "published"})
                processed_urls.add(item["link"])
                processed_count += 1
                
    if new_article_created:
        generate_weekly_book()
        rebuild_index_and_rotate_storage()
        save_history(history)
    else:
        rebuild_index_and_rotate_storage()

if __name__ == '__main__':
    main()
