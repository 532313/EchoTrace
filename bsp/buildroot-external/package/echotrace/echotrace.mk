################################################################################
# EchoTrace: source stays separate from the vendor SDK.
################################################################################
ECHOTRACE_VERSION = 0.1.0
ECHOTRACE_SITE = $(call qstrip,$(BR2_PACKAGE_ECHOTRACE_SOURCE))
ECHOTRACE_SITE_METHOD = local
ECHOTRACE_DEPENDENCIES = alsa-lib
ECHOTRACE_OVERRIDE_SRCDIR_RSYNC_EXCLUSIONS = --exclude=models/ --exclude=runtime/ --exclude=.venv/ --exclude=.convert-venv/ --exclude=.rk-convert/ --exclude=.rknn-convert/ --exclude=build/host/ --exclude=build/rv1106/ --exclude=build/rv1106-deskbot-mode/ --exclude=build/deskbot-integration*/ --exclude=build/rk3576/ --exclude=build/wheels-convert/ --exclude=build/research/ --exclude=build/qwen-language/ --exclude=build/qwen-vision/ --exclude=build/npm-cache/ --exclude=build/rkllm/ --exclude=build/artifacts/ --exclude=build/bsp-*/ --exclude=build/*.tar.gz --exclude=build/*.token
ECHOTRACE_CONF_OPTS = -DECHO_RV1106=ON \
    -DECHO_BUILD_QT=OFF -DECHO_BUILD_VLM=OFF \
    -DECHO_MEDIA_SOURCE_ROOT=$(call qstrip,$(BR2_PACKAGE_ECHOTRACE_MEDIA_ROOT)) \
    -DECHO_LVGL_ROOT=$(call qstrip,$(BR2_PACKAGE_ECHOTRACE_LVGL_ROOT)) \
    -DECHO_DEPS_DIR=$(@D)/build/deps -DECHO_VENDOR_DIR=$(@D)/build/vendor
ifeq ($(BR2_PACKAGE_ECHOTRACE_DESKBOT),y)
ECHOTRACE_CONF_OPTS += -DECHO_BUILD_LVGL=OFF
define ECHOTRACE_INSTALL_DISPLAY_CMDS
    test -x "$(call qstrip,$(BR2_PACKAGE_ECHOTRACE_DESKBOT_BIN))/main"
    test -f "$(call qstrip,$(BR2_PACKAGE_ECHOTRACE_DESKBOT_BIN))/model/yolov5.rknn"
    test -d "$(call qstrip,$(BR2_PACKAGE_ECHOTRACE_DESKBOT_BIN))/lib"
    test -d "$(call qstrip,$(BR2_PACKAGE_ECHOTRACE_DESKBOT_BIN))/third_party"
    $(INSTALL) -d $(TARGET_DIR)/opt/echotrace/deskbot/bin
    cp -a "$(call qstrip,$(BR2_PACKAGE_ECHOTRACE_DESKBOT_BIN))/." $(TARGET_DIR)/opt/echotrace/deskbot/bin/
endef
define ECHOTRACE_INSTALL_DISPLAY_INIT_CMDS
    rm -f $(TARGET_DIR)/etc/init.d/S96echotrace-terminal
    $(INSTALL) -D -m 0755 $(@D)/bsp/rv1106/S96echotrace-deskbot $(TARGET_DIR)/etc/init.d/S96echotrace-deskbot
endef
else
ECHOTRACE_CONF_OPTS += -DECHO_BUILD_LVGL=ON
define ECHOTRACE_INSTALL_DISPLAY_CMDS
    $(INSTALL) -D -m 0755 $(@D)/echotrace-terminal $(TARGET_DIR)/opt/echotrace/bin/echotrace-terminal
endef
define ECHOTRACE_INSTALL_DISPLAY_INIT_CMDS
    rm -f $(TARGET_DIR)/etc/init.d/S96echotrace-deskbot
    $(INSTALL) -D -m 0755 $(@D)/bsp/rv1106/S96echotrace-terminal $(TARGET_DIR)/etc/init.d/S96echotrace-terminal
endef
endif
# Media libraries are provided by the matching vendor image; they are not replaced.
define ECHOTRACE_INSTALL_TARGET_CMDS
    $(INSTALL) -D -m 0755 $(@D)/echotrace-edge $(TARGET_DIR)/opt/echotrace/bin/echotrace-edge
    $(ECHOTRACE_INSTALL_DISPLAY_CMDS)
    $(INSTALL) -D -m 0644 $(@D)/config/edge.example.json $(TARGET_DIR)/etc/echotrace/edge.json
endef

define ECHOTRACE_INSTALL_INIT_SYSV
    $(INSTALL) -D -m 0755 $(@D)/bsp/rv1106/S95echotrace $(TARGET_DIR)/etc/init.d/S95echotrace
    $(ECHOTRACE_INSTALL_DISPLAY_INIT_CMDS)
endef

$(eval $(cmake-package))
