import io
import os

import requests
import streamlit as st
from PIL import Image, ImageDraw, ImageFont

ROBOFLOW_API_URL = "https://serverless.roboflow.com"

st.set_page_config(page_title="胚検出・評価AIアプリ", layout="centered")
st.title("胚検出・評価AIシステム")
st.write("画像をアップロードすると、AIが自動で解析して評価・検出を行います。")


def get_model_id() -> str:
    if "ROBOFLOW_MODEL_ID" in st.secrets:
        return st.secrets["ROBOFLOW_MODEL_ID"]
    return os.environ.get(
        "ROBOFLOW_MODEL_ID",
        "embryo_detection-chld1-2-rfdetr-small-t1/2",
    )


def get_workspace() -> str:
    if "ROBOFLOW_WORKSPACE" in st.secrets:
        return st.secrets["ROBOFLOW_WORKSPACE"]
    return os.environ.get("ROBOFLOW_WORKSPACE", "")


def get_api_key() -> str:
    if "ROBOFLOW_API_KEY" in st.secrets:
        return st.secrets["ROBOFLOW_API_KEY"]
    return os.environ.get("ROBOFLOW_API_KEY", "")


MODEL_ID = get_model_id()
WORKSPACE = get_workspace()
API_KEY = get_api_key()

parts = [part for part in MODEL_ID.strip("/").split("/") if part]
if len(parts) not in (2, 3):
    st.error(
        "モデル ID の形式が正しくありません。"
        "次のいずれかの形式で指定してください。"
    )
    st.code(
        'ROBOFLOW_MODEL_ID = "プロジェクト名/バージョン"\n'
        'ROBOFLOW_MODEL_ID = "ワークスペース/プロジェクト名/バージョン"'
    )
    st.stop()

if len(parts) == 2 and not WORKSPACE:
    st.info(
        "ヒント: HTTP 404 が出る場合は、Roboflow の Deploy 画面から "
        "`ワークスペース/プロジェクト/バージョン` 形式の ID をコピーするか、"
        "Secrets に `ROBOFLOW_WORKSPACE` を追加してください。"
    )

if not API_KEY or API_KEY == "API_KEY":
    st.warning("Roboflow APIキーが設定されていません。")
    st.info(
        "Streamlit Cloud の Secrets または `.streamlit/secrets.toml` に次を追加してください:\n\n"
        'ROBOFLOW_API_KEY = "あなたのAPIキー"'
    )
    st.stop()


def build_inference_urls(model_id: str, workspace: str = "") -> list[str]:
    segments = [part for part in model_id.strip("/").split("/") if part]
    urls: list[str] = []

    if len(segments) == 3:
        ws, project, version = segments
        urls.append(f"{ROBOFLOW_API_URL}/infer/{ws}/{project}/{version}")
        urls.append(f"{ROBOFLOW_API_URL}/{project}/{version}")
    elif len(segments) == 2:
        project, version = segments
        if workspace:
            urls.append(f"{ROBOFLOW_API_URL}/infer/{workspace}/{project}/{version}")
        urls.append(f"{ROBOFLOW_API_URL}/{project}/{version}")

    seen: set[str] = set()
    unique_urls: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique_urls.append(url)
    return unique_urls


def load_uploaded_image(uploaded_file) -> Image.Image:
    uploaded_file.seek(0)
    image = Image.open(io.BytesIO(uploaded_file.read()))
    if image.mode != "RGB":
        image = image.convert("RGB")
    return image


def image_to_jpeg_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def run_inference(
    image: Image.Image,
    api_key: str,
    model_id: str,
    workspace: str = "",
) -> dict:
    img_bytes = image_to_jpeg_bytes(image)
    last_response = None

    for url in build_inference_urls(model_id, workspace):
        response = requests.post(
            url,
            params={"api_key": api_key, "confidence": 10}, 
            files={"file": ("image.jpg", img_bytes, "image/jpeg")},
            timeout=120,
        )
        if response.status_code == 404:
            last_response = response
            continue
        response.raise_for_status()
        return response.json()

    if last_response is not None:
        last_response.raise_for_status()
    raise requests.HTTPError("推論 URL が見つかりませんでした。")


def extract_predictions(result) -> list:
    if isinstance(result, list):
        if result and isinstance(result[0], dict):
            return result[0].get("predictions", [])
        return []

    if isinstance(result, dict):
        if "predictions" in result:
            return result.get("predictions", [])
        nested = result.get("result")
        if isinstance(nested, dict):
            return nested.get("predictions", [])

    return []


def prediction_bbox(pred) -> tuple[float, float, float, float] | None:
    """Roboflow の bbox を (x_min, y_min, x_max, y_max) に統一"""
    if not isinstance(pred, dict):
        return None

    if all(key in pred for key in ("x_min", "y_min", "x_max", "y_max")):
        return (
            float(pred["x_min"]),
            float(pred["y_min"]),
            float(pred["x_max"]),
            float(pred["y_max"]),
        )

    if all(key in pred for key in ("x", "y", "width", "height")):
        cx = float(pred["x"])
        cy = float(pred["y"])
        w = float(pred["width"])
        h = float(pred["height"])
        return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)

    return None


def box_area(box: tuple[float, float, float, float]) -> float:
    x1, y1, x2, y2 = box
    return max(0.0, x2 - x1) * max(0.0, y2 - y1)


def intersection_area(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    inter_w = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    inter_h = max(0.0, min(ay2, by2) - max(ay1, by1))
    return inter_w * inter_h


def bbox_iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    inter_area = inter_w * inter_h
    if inter_area <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter_area
    if union <= 0:
        return 0.0
    return inter_area / union


def is_same_embryo(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    *,
    iou_threshold: float = 0.92,
) -> bool:
    """ほぼ完全に重なった枠だけを同一胚とみなす。見た目や評価名が似ていても別の胚は残す。"""
    return bbox_iou(a, b) >= iou_threshold


def drop_group_boxes(kept: list) -> list:
    """複数の胚をまとめて覆っている大きな枠だけを外す。"""
    if len(kept) < 3:
        return kept

    drop_indexes: set[int] = set()
    for i, (_, outer, _) in enumerate(kept):
        outer_area = box_area(outer)
        if outer_area <= 0:
            continue
        contained = 0
        for j, (_, inner, _) in enumerate(kept):
            if i == j:
                continue
            inner_area = box_area(inner)
            if inner_area <= 0 or inner_area >= outer_area * 0.85:
                continue
            if intersection_area(outer, inner) / inner_area >= 0.8:
                contained += 1
        if contained >= 2:
            drop_indexes.add(i)

    return [item for i, item in enumerate(kept) if i not in drop_indexes]


def dedupe_predictions(
    predictions: list,
    *,
    min_confidence: float = 0.1,
) -> list:
    """
    同じ胚への二重検出だけを除く。
    隣り合う別の胚や、画像内の多数の胚は残す。
    """
    candidates = []
    without_box = []
    for pred in predictions or []:
        if not isinstance(pred, dict):
            continue
        confidence = float(pred.get("confidence", 0) or 0)
        if confidence < min_confidence:
            continue
        box = prediction_bbox(pred)
        if box is None:
            without_box.append(pred)
            continue
        candidates.append((confidence, box, pred))

    candidates.sort(key=lambda item: item[0], reverse=True)
    kept: list = []
    for confidence, box, pred in candidates:
        if any(is_same_embryo(box, kept_box) for _, kept_box, _ in kept):
            continue
        kept.append((confidence, box, pred))

    kept = drop_group_boxes(kept)
    return [pred for _, _, pred in kept] + without_box


GRADE_COLORS = [
    (220, 38, 38),
    (37, 99, 235),
    (22, 163, 74),
    (217, 119, 6),
    (124, 58, 237),
    (8, 145, 178),
    (190, 24, 93),
    (71, 85, 105),
]


def sort_predictions_for_display(predictions: list) -> list:
    def sort_key(pred):
        box = prediction_bbox(pred)
        if box is None:
            return (10**9, 10**9)
        x1, y1, x2, y2 = box
        return ((y1 + y2) / 2, (x1 + x2) / 2)

    return sorted(predictions, key=sort_key)


def load_label_font(image_width: int):
    size = max(18, image_width // 45)
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial.ttf",
    ):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def text_pixel_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    if hasattr(draw, "textbbox"):
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        return right - left, bottom - top
    width, height = draw.textsize(text, font=font)
    return width, height


def annotate_embryos(image: Image.Image, predictions: list) -> Image.Image:
    annotated = image.copy()
    draw = ImageDraw.Draw(annotated)
    width, height = annotated.size
    font = load_label_font(width)
    line_width = max(3, width // 280)

    grades = []
    for pred in predictions:
        grade = str(pred.get("class", "判定不能"))
        if grade not in grades:
            grades.append(grade)
    color_of = {grade: GRADE_COLORS[i % len(GRADE_COLORS)] for i, grade in enumerate(grades)}

    for index, pred in enumerate(predictions, start=1):
        box = prediction_bbox(pred)
        if box is None:
            continue
        x1, y1, x2, y2 = box
        x1 = max(0, min(width - 1, int(round(x1))))
        y1 = max(0, min(height - 1, int(round(y1))))
        x2 = max(0, min(width - 1, int(round(x2))))
        y2 = max(0, min(height - 1, int(round(y2))))
        if x2 <= x1 or y2 <= y1:
            continue

        grade = str(pred.get("class", "判定不能"))
        color = color_of.get(grade, GRADE_COLORS[0])
        draw.rectangle([x1, y1, x2, y2], outline=color, width=line_width)

        label = f"{index} {grade}"
        text_w, text_h = text_pixel_size(draw, label, font)
        pad = 4
        label_top = y1 - text_h - pad * 2
        if label_top < 0:
            label_top = y1
        label_box = [x1, label_top, min(width - 1, x1 + text_w + pad * 2), label_top + text_h + pad * 2]
        draw.rectangle(label_box, fill=color)
        draw.text((label_box[0] + pad, label_box[1] + pad), label, fill=(255, 255, 255), font=font)

    return annotated


uploaded_file = st.file_uploader(
    "胚の画像を選択してください（JPG, JPEG, PNG）",
    type=["jpg", "jpeg", "png"],
)

if uploaded_file is not None:
    try:
        image = load_uploaded_image(uploaded_file)
    except Exception as e:
        st.error(f"画像の読み込みに失敗しました: {e}")
        st.stop()

    st.image(image, caption="アップロードされた画像", use_container_width=True)

    if st.button("AIで解析・評価する", type="primary"):
        with st.spinner("AIが画像を解析中..."):
            try:
                result = run_inference(image, API_KEY, MODEL_ID, WORKSPACE)
                raw_predictions = extract_predictions(result)
                predictions = sort_predictions_for_display(dedupe_predictions(raw_predictions))

                st.success("解析が完了しました！")

                if not predictions:
                    if raw_predictions:
                        st.info(
                            "重なった検出結果を整理しましたが、有効な胚は見つかりませんでした。"
                            "別の画像でお試しください。"
                        )
                    else:
                        st.info("対象の胚は検出されませんでした。")
                else:
                    st.subheader("AI診断結果")
                    st.caption(
                        f"評価した胚: **{len(predictions)} 個**。"
                        "同じ評価名（例: EB-A）でも、位置が違えば別の胚です。"
                        "画像の番号と下の一覧が対応しています。"
                    )

                    annotated = annotate_embryos(image, predictions)
                    st.image(annotated, caption="番号と評価を枠で表示", use_container_width=True)

                    for index, pred in enumerate(predictions, start=1):
                        grade = pred.get("class", "判定不能")
                        confidence = float(pred.get("confidence", 0) or 0) * 100
                        st.markdown(
                            f"**{index}. 【 {grade} 】**　確信度 {confidence:.1f}%"
                        )

                    with st.expander("詳細な解析データを見る（開発者・データ確認用）"):
                        st.write(f"評価数: {len(predictions)} 個")
                        st.write(f"AI生検出数: {len(raw_predictions)} 個")
                        st.json(result)

            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                st.error(f"解析中にエラーが発生しました: HTTP {status}")
                if status == 401:
                    st.warning(
                        "APIキーが無効、または Serverless 推論に対応していません。"
                        "Roboflow の [Settings → API](https://app.roboflow.com/settings/api) "
                        "から **Private API Key** を Secrets に設定してください。"
                    )
                elif status == 404:
                    st.warning(
                        "モデル ID が見つかりません。Roboflow で次を確認してください:\n\n"
                        "1. プロジェクト → **Deploy** → **Hosted API** を開く\n"
                        "2. 表示されている **Model ID** または URL をコピー\n"
                        "3. Secrets の `ROBOFLOW_MODEL_ID` を更新\n\n"
                        "例:\n"
                        '- `ワークスペース名/プロジェクト名/2`\n'
                        '- または `ROBOFLOW_WORKSPACE` と `プロジェクト名/2` を分けて設定'
                    )
                    with st.expander("現在の設定"):
                        st.write(f"MODEL_ID: `{MODEL_ID}`")
                        st.write(f"WORKSPACE: `{WORKSPACE or '未設定'}`")
                        st.write("試行 URL:")
                        for url in build_inference_urls(MODEL_ID, WORKSPACE):
                            st.code(url)
            except requests.RequestException as e:
                st.error(f"通信エラーが発生しました: {e}")
            except Exception as e:
                st.error(f"解析中にエラーが発生しました: {e}")
