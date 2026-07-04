import io
import os

import requests
import streamlit as st
from PIL import Image

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
            params={"api_key": api_key},
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
                predictions = extract_predictions(result)

                st.success("解析が完了しました！")

                if not predictions:
                    st.info("対象のオブジェクトは検出されませんでした。")
                else:
                    st.subheader("検出結果（評価データ）")
                    st.write(f"検出数: {len(predictions)} 個")
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
