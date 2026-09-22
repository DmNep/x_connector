x_connector — установка агента с этой флешки

На сервере (клавиатура и монитор ещё подключены, интернета нет и не нужно):

    sudo mkdir -p /mnt/usb
    sudo mount /dev/sdX1 /mnt/usb
    cd /mnt/usb
    sudo sh install.sh

На флешке лежит автономный Linux python3 (x86_64 и aarch64)
и alsa-utils (aplay/arecord/amixer) для Ubuntu 22.04, 24.04 и 26.04.
install.sh ставит python в /opt/x_connector/python и, если нет aplay,
пакеты из alsa-debs/. Системный python3 не требуется. apt и pip
не вызываются. Повторный install.sh не сбрасывает hw: из
/etc/default/xconn-agent.

Устройства звука:

    arecord -l
    aplay -l

Прописать в /etc/default/xconn-agent (пример hw:1,0):

    XCONN_CAPTURE=hw:1,0
    XCONN_PLAYBACK=hw:1,0

Запуск:

    sudo systemctl start xconn-agent
    sudo systemctl status xconn-agent

Кабели по схеме X, на ноутбуке:

    py -m xconn_channel client --repl
