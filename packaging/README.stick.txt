x_connector — установка агента с этой флешки

На сервере (клавиатура и монитор ещё подключены, интернета нет и не нужно):

    sudo mkdir -p /mnt/usb
    sudo mount /dev/sdX1 /mnt/usb
    cd /mnt/usb
    sudo sh install.sh

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

Если на сервере нет python3, на ноутбуке (где интернет есть) скачайте
пакеты python3 и зависимости в каталог python-debs/ на этой флешке.
install.sh поставит их через dpkg -i, без apt и без сети.
