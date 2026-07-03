import streamlit as st
from inference_sdk import InferenceHTTPClient
from PIL import Image
import io

# ページの設定
st.set_page_config(page_title="胚検出・評価AIアプリ", layout="centered")
st.title("🔬 胚検出・評価AIシステム")
st.write("画像をアップロードすると、AIが自動で解析して評価・検出を行います。")

# --- Roboflowの設定 ---
# 安全のため、実際の運用ではStreamlitのSecrets機能を使うのが推奨されますが、まずは直接入力でテスト可能です
API_KEY = "API_KEY" 
MODEL_ID = "embryo_detection-chld1-2-rfdetr-small-t1/2" # もし違っていたら書き換えてください

# クライアントの初期化
@st.cache_resource
def get_inference_client():
    return InferenceHTTPClient(
        api_url="https://serverless.roboflow.com",
        api_key=API_KEY
    )

client = get_inference_client()

# --- 画像のアップロード画面 ---
uploaded_file = st.file_uploader("胚の画像を選択してください（JPG, JPEG, PNG）", type=["jpg", "jpeg", "png"])

if uploaded_file is not None:
    # アップロードされた画像を表示
    image = Image.open(uploaded_file)
    st.image(image, caption="アップロードされた画像", use_container_width=True)
    
    # 解析ボタン
    if st.button("AIで解析・評価する", type="primary"):
        with st.spinner("AIが画像を解析中..."):
            try:
                # PIL画像をバイトデータに変換してRoboflowに送信
                img_byte_arr = io.BytesIO()
                image.save(img_byte_arr, format=image.format if image.format else "JPEG")
                img_bytes = img_byte_arr.getvalue()
                
                # AI推論の実行
                # inference_sdkは、ローカルのファイルパスだけでなく、画像オブジェクトやバイトデータもそのまま渡せます
                result = client.infer(image, model_id=MODEL_ID)
                
                # --- 結果の表示 ---
                st.success("解析が完了しました！")
                
                # 検出されたオブジェクト（予測データ）の確認
                predictions = result.get("predictions", [])
                
                if not predictions:
                    st.info("対象のオブジェクトは検出されませんでした。")
                else:
                    st.subheader("📊 検出結果（評価データ）")
                    st.write(f"検出数: {len(predictions)} 個")
                    
                    # データを綺麗に表示（JSON、またはカスタマイズして表形式など）
                    st.json(result)
                    
            except Exception as e:
                st.error(f"解析中にエラーが発生しました: {e}")
