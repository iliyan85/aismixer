#
# OpenWrt packaging for AISMixer and nmea_sproxy
#

include $(TOPDIR)/rules.mk

PKG_NAME:=aismixer
PKG_VERSION:=0.2.1
PKG_RELEASE:=1

# Pin the task-start R2.1 runtime revision; do not follow a moving branch.
# At final release closure, repin the source revision and mirror hash together.
PKG_SOURCE_PROTO:=git
PKG_SOURCE_URL:=https://github.com/iliyan85/aismixer.git
PKG_SOURCE_DATE:=2026-08-25
PKG_SOURCE_VERSION:=cd68b202e362712fe0503ea2b4b8d55ef88a609a

PKG_MIRROR_HASH:=5e7136994d302df0399841e2e6aba3ce48889f21510dac2c8197d8f27413c941

PKG_MAINTAINER:=Iliyan Iliev
PKG_LICENSE:=CC-BY-NC-4.0
PKG_LICENSE_FILES:=LICENSE

# The current AISMixer payload is Python/shell only.
# Native C components introduced later should become target-specific packages.
PKGARCH:=all

include $(INCLUDE_DIR)/package.mk


define Package/aismixer/Default
  SECTION:=net
  CATEGORY:=Network
  URL:=https://aismixer.net/
endef


define Package/aismixer-common
  $(call Package/aismixer/Default)
  TITLE:=Shared AISMixer runtime modules
  SOURCE:=github.com/iliyan85/aismixer
  DEPENDS:=+python3
endef

define Package/aismixer-common/description
 Shared Python data-plane, routing, network-policy, UDPSEC protocol,
 cryptographic helper modules, and common AISMixer utilities.
endef


define Package/aismixer
  $(call Package/aismixer/Default)
  TITLE:=AIS/NMEA mixer, deduplicator and router
  SOURCE:=github.com/iliyan85/aismixer
  DEPENDS:=+aismixer-common +python3-yaml +python3-cryptography
endef

define Package/aismixer/description
 AISMixer combines AIS/NMEA inputs, performs assembly, deduplication,
 TAG handling and routing, and forwards the resulting logical stream
 to selected outputs. Includes UDPSEC server support and aismixerctl.
endef


# The matching OpenWrt 25.12 feeds do not provide Python setproctitle.
# Named relation identity is therefore exposed through procd instance names,
# not OS process titles.
define Package/nmea_sproxy
  $(call Package/aismixer/Default)
  TITLE:=AIS/NMEA serial and UDP secure proxy
  SOURCE:=github.com/iliyan85/aismixer
  DEPENDS:=+aismixer-common +python3-yaml +python3-cryptography +python3-pyserial
endef

define Package/nmea_sproxy/description
 nmea_sproxy forwards one local AIS/NMEA input, using UDP or serial,
 to one network output using authenticated UDPSEC or plain UDP.
endef


define Package/aismixer/conffiles
/etc/aismixer/config.yaml
/etc/aismixer/udp_alias_map.yaml
/etc/aismixer/authorized_keys.yaml
endef


define Package/nmea_sproxy/conffiles
/etc/nmea_sproxy/config.yaml
endef


# Pure Python package: there is currently nothing to compile.
define Build/Compile
endef


define Package/aismixer-common/install
	$(INSTALL_DIR) $(1)/usr/lib/aismixer/core
	$(CP) $(PKG_BUILD_DIR)/core/. \
		$(1)/usr/lib/aismixer/core/

	$(INSTALL_DIR) $(1)/usr/lib/aismixer/tools
	$(INSTALL_DATA) $(PKG_BUILD_DIR)/tools/aismixer_keys.py \
		$(1)/usr/lib/aismixer/tools/aismixer_keys.py
endef


define Package/aismixer/install
	$(INSTALL_DIR) $(1)/usr/lib/aismixer
	$(CP) $(PKG_BUILD_DIR)/*.py \
		$(1)/usr/lib/aismixer/

	$(INSTALL_DIR) $(1)/usr/bin
	$(INSTALL_BIN) ./files/aismixer \
		$(1)/usr/bin/aismixer
	$(INSTALL_BIN) ./files/aismixerctl \
		$(1)/usr/bin/aismixerctl

	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_BIN) ./files/aismixer.init \
		$(1)/etc/init.d/aismixer

	$(INSTALL_DIR) $(1)/etc/aismixer
	$(INSTALL_CONF) ./files/config.yaml \
		$(1)/etc/aismixer/config.yaml
	$(INSTALL_CONF) $(PKG_BUILD_DIR)/udp_alias_map.yaml \
		$(1)/etc/aismixer/udp_alias_map.yaml
	$(INSTALL_CONF) ./files/authorized_keys.yaml \
		$(1)/etc/aismixer/authorized_keys.yaml

	# Identity material is generated locally on the router.
	# Private keys must never be shipped inside the package.
	$(INSTALL_DIR) $(1)/etc/aismixer/keys
endef


define Package/nmea_sproxy/install
	$(INSTALL_DIR) $(1)/usr/lib/aismixer/nmea_sproxy
	$(CP) $(PKG_BUILD_DIR)/nmea_sproxy/*.py \
		$(1)/usr/lib/aismixer/nmea_sproxy/

	$(INSTALL_DIR) $(1)/usr/bin
	$(INSTALL_BIN) ./files/nmea_sproxy \
		$(1)/usr/bin/nmea_sproxy

	$(INSTALL_DIR) $(1)/etc/init.d
	$(INSTALL_BIN) ./files/nmea_sproxy.init \
		$(1)/etc/init.d/nmea_sproxy

	$(INSTALL_DIR) $(1)/etc/nmea_sproxy
	$(INSTALL_CONF) ./files/nmea_sproxy.config.yaml \
		$(1)/etc/nmea_sproxy/config.yaml

	# Each regular YAML file placed here by the operator becomes one named
	# procd instance. The package intentionally ships this directory empty.
	$(INSTALL_DIR) $(1)/etc/nmea_sproxy/instances

	# Station identity and trusted server public key are provisioned
	# locally and are not package payload.
	$(INSTALL_DIR) $(1)/etc/nmea_sproxy/keys
	chmod 0700 $(1)/etc/nmea_sproxy/keys
endef


$(eval $(call BuildPackage,aismixer-common))
$(eval $(call BuildPackage,aismixer))
$(eval $(call BuildPackage,nmea_sproxy))
