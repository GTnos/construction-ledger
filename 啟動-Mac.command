#!/bin/bash
cd "$(dirname "$0")/ledger"

if command -v python3 >/dev/null 2>&1; then
  PYCMD=python3
elif command -v python >/dev/null 2>&1; then
  PYCMD=python
else
  echo ""
  echo "找不到 Python 3，請先安裝："
  echo "到 https://www.python.org/downloads/ 下載安裝，裝完後再雙擊本檔案一次。"
  echo ""
  read -p "按 Enter 結束"
  exit 1
fi

MYIP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)

echo "============================================"
echo " 工程收支帳務系統伺服器啟動中..."
echo ""
echo " 這台電腦上瀏覽器打開： http://localhost:8123"
if [ -n "$MYIP" ]; then
  echo " 手機（同一個 Wi-Fi）打開： http://$MYIP:8123"
else
  echo " 手機（同一個 Wi-Fi）打開： http://電腦的區網IP:8123"
  echo "   （查法：系統設定 → 網路 → 看目前連線的 IP 位址）"
fi
echo ""
echo " 這個視窗請不要關閉，關閉視窗伺服器就會停止。"
echo " 要停止伺服器：直接關閉本視窗，或按 Ctrl+C。"
echo "============================================"
echo ""
"$PYCMD" server.py 8123
echo ""
echo "伺服器已停止。"
read -p "按 Enter 結束"
