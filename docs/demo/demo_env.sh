# Sourced off-camera by demo.tape: prompt, scene captions, background backend.
export PS1='$ ' HISTFILE=/dev/null
S="rec$RANDOM"
scene() { clear; printf '\033[1;35m▶ %s\033[0m\n\033[2m%s\033[0m\n\n' "$1" "$2"; }
python3 -m hr_helpdesk.server > /tmp/hr_demo_server.log 2>&1 &
sleep 2
clear
