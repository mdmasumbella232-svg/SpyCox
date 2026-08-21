#!/bin/bash
# SpyCox - Basketball Prediction Bot Launcher

pip install pyTelegramBotAPI requests -q 2>/dev/null
echo "Starting SpyCox..."
exec python3 "$(dirname "$0")/prediction_bot.py"
