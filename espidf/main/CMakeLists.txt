idf_component_register(SRCS "app_main.c" "protocol.c" "printer.c" "keyboard_map.c" "usb_keyboard.c" INCLUDE_DIRS "."
                       REQUIRES kxr60 esp_driver_uart esp_driver_usb_serial_jtag
                                vfs esp_system usb_host_hid)
