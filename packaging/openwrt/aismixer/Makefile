#
# OpenWrt packaging for AISMixer and nmea_sproxy
#

include $(TOPDIR)/rules.mk

PKG_NAME:=aismixer
PKG_VERSION:=0.2.0
PKG_RELEASE:=1

# Pin the exact upstream source revision used for the first OpenWrt
# packaging/runtime validation. Do not follow a moving branch here.
PKG_SOURCE_PROTO:=git
PKG_SOURCE_URL:=https://github.com/iliyan85/aismixer.git
PKG_SOURCE_DATE:=2026-08-13
PKG_SOURCE_VERSION:=10df1265b4226debe06815202ed88a4010a567fc

# Add PKG_MIRROR_HASH before treating this as a reproducible public package.
PKG_MIRROR_HASH:=f4cf3d8fa68b338b58db38d4919d6f01c4027bef2b7db3108d48ad9c30db10c1

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
  DEPENDS:=+python3
endef

define Package/aismixer-common/description
 Shared Python data-plane, routing, network-policy, UDPSEC protocol,
 cryptographic helper modules, and common AISMixer utilities.
endef


define Package/aismixer
  $(call Package/aismixer/Default)
  TITLE:=AIS/NMEA mixer, deduplicator and router
  DEPENDS:=+aismixer-common +python3-yaml +python3-cryptography
endef

define Package/aismixer/description
 AISMixer combines AIS/NMEA inputs, performs assembly, deduplication,
 TAG handling and routing, and forwards the resulting logical stream
 to selected outputs. Includes UDPSEC server support and aismixerctl.
endef


define Package/nmea_sproxy
  $(call Package/aismixer/Default)
  TITLE:=AIS/NMEA serial and UDP secure proxy
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
	$(INSTALL_CONF) $(PKG_BUILD_DIR)/nmea_sproxy/config.system.yaml \
		$(1)/etc/nmea_sproxy/config.yaml

	# Station identity and trusted server public key are provisioned
	# locally and are not package payload.
	$(INSTALL_DIR) $(1)/etc/nmea_sproxy/keys
endef


$(eval $(call BuildPackage,aismixer-common))
$(eval $(call BuildPackage,aismixer))
$(eval $(call BuildPackage,nmea_sproxy))
