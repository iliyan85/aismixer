#pragma once

#include "communication_state.h"

#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <variant>

namespace aismixer::lab::ais {

inline constexpr std::int64_t kLongitudeUnavailable = 108600000;
inline constexpr std::int64_t kLatitudeUnavailable = 54600000;

// Explicit nine-digit identity, including leading zeros. This codec rejects
// all-zero identities; it does not verify an allocation or derive a prefix.
class Mmsi {
public:
    explicit Mmsi(std::string_view digits) {
        if (digits.size() != 9) {
            throw std::invalid_argument("MMSI: exactly nine ASCII decimal digits are required");
        }
        std::uint64_t value = 0;
        for (const char digit : digits) {
            if (digit < '0' || digit > '9') {
                throw std::invalid_argument("MMSI: exactly nine ASCII decimal digits are required");
            }
            value = value * 10 + static_cast<unsigned>(digit - '0');
        }
        if (value == 0 || value > UINT64_C(0x3fffffff)) {
            throw std::invalid_argument("MMSI: nonzero identity must fit the 30-bit wire field");
        }
        digits_ = std::string(digits);
        value_ = static_cast<std::uint32_t>(value);
    }

    const std::string& digits() const noexcept { return digits_; }
    std::uint32_t wire_value() const noexcept { return value_; }

private:
    std::string digits_;
    std::uint32_t value_ = 0;
};

// Communication-state bits must be deliberately supplied. No default state,
// slot allocation, synchronization or TDMA operation is inferred by the codec.
class RadioState19 {
public:
    explicit RadioState19(std::int64_t value) {
        if (value < 0 || value > 0x7ffff) {
            throw std::invalid_argument("RadioState19: raw communication state must fit 19 unsigned bits");
        }
        value_ = static_cast<std::uint32_t>(value);
    }
    std::uint32_t value() const noexcept { return value_; }

private:
    std::uint32_t value_;
};

// High bit is the communication-state selector, low 19 bits are the state.
class RadioState20 {
public:
    explicit RadioState20(std::int64_t value) {
        if (value < 0 || value > 0xfffff) {
            throw std::invalid_argument("RadioState20: selector/state must fit 20 unsigned bits");
        }
        value_ = static_cast<std::uint32_t>(value);
    }
    std::uint32_t value() const noexcept { return value_; }

private:
    std::uint32_t value_;
};

// Numeric inputs use int64_t so negative/oversized unsigned-field values remain
// visible to validation instead of being narrowed into apparently valid data.
// Geographic coordinates use 1/10000 minute; they are not local millimetres.
struct Type1 {
    Type1(Mmsi identity, RadioState19 state)
        : mmsi(std::move(identity)), radio_state(state) {}

    Mmsi mmsi;
    std::int64_t repeat_indicator = 0;
    std::int64_t navigation_status = 15;  // Undefined.
    std::int64_t rot_wire = -128;  // Signed AIS ROT code, unavailable; not deg/min.
    std::int64_t sog_tenths_knots = 1023;
    bool position_accuracy = false;
    std::int64_t longitude = kLongitudeUnavailable;
    std::int64_t latitude = kLatitudeUnavailable;
    std::int64_t cog_tenths_degrees = 3600;
    std::int64_t true_heading_degrees = 511;
    std::int64_t utc_second = 60;
    std::int64_t special_manoeuvre = 0;
    bool low_transmit_power = false;  // M.1371-6 bit 147.
    bool raim = false;
    RadioState19 radio_state;
};

struct Type4 {
    Type4(Mmsi identity, RadioState19 state)
        : mmsi(std::move(identity)), radio_state(state) {}

    Mmsi mmsi;
    std::int64_t repeat_indicator = 0;
    std::int64_t utc_year = 0;
    std::int64_t utc_month = 0;
    std::int64_t utc_day = 0;
    std::int64_t utc_hour = 24;
    std::int64_t utc_minute = 60;
    std::int64_t utc_second = 60;
    bool position_accuracy = false;
    std::int64_t longitude = kLongitudeUnavailable;
    std::int64_t latitude = kLatitudeUnavailable;
    std::int64_t position_fixing_device = 0;  // Undefined EPFS.
    bool satellite_transmission_control = false;
    bool raim = false;
    RadioState19 radio_state;
};

// This is explicitly a SAR aircraft report, never inferred from a SAR actor.
struct Type9 {
    Type9(Mmsi identity, RadioState20 state)
        : mmsi(std::move(identity)), radio_state(state) {}

    Mmsi mmsi;
    std::int64_t repeat_indicator = 0;
    std::int64_t altitude_m = 4095;
    std::int64_t sog_knots = 1023;  // Whole knots, unlike Types 1/18.
    bool position_accuracy = false;
    std::int64_t longitude = kLongitudeUnavailable;
    std::int64_t latitude = kLatitudeUnavailable;
    std::int64_t cog_tenths_degrees = 3600;
    std::int64_t utc_second = 60;
    bool altitude_barometric = false;
    bool dte_not_ready = true;
    bool assigned_mode = false;
    bool raim = false;
    RadioState20 radio_state;
};

struct Type18 {
    Type18(Mmsi identity, RadioState20 state)
        : mmsi(std::move(identity)), radio_state(state) {}

    Mmsi mmsi;
    std::int64_t repeat_indicator = 0;
    std::int64_t sog_tenths_knots = 1023;
    bool position_accuracy = false;
    std::int64_t longitude = kLongitudeUnavailable;
    std::int64_t latitude = kLatitudeUnavailable;
    std::int64_t cog_tenths_degrees = 3600;
    std::int64_t true_heading_degrees = 511;
    std::int64_t utc_second = 60;
    bool low_transmit_power = false;  // M.1371-6 bit 139.
    bool cs_unit = false;
    bool display = false;
    bool dsc = false;
    bool whole_band = false;
    bool message22 = false;
    bool assigned_mode = false;
    bool raim = false;
    RadioState20 radio_state;
};

// Base 272-bit report only; no optional name extension is emitted in B3.
struct Type21 {
    explicit Type21(Mmsi identity) : mmsi(std::move(identity)) {}

    Mmsi mmsi;
    std::int64_t repeat_indicator = 0;
    std::int64_t aid_type = 0;
    std::string name;  // 0..20 ASCII characters, 0x20..0x5F excluding '@'.
    bool position_accuracy = false;
    std::int64_t longitude = kLongitudeUnavailable;
    std::int64_t latitude = kLatitudeUnavailable;
    std::int64_t dimension_a = 0;
    std::int64_t dimension_b = 0;
    std::int64_t dimension_c = 0;
    std::int64_t dimension_d = 0;
    std::int64_t position_fixing_device = 0;
    std::int64_t utc_second = 60;
    bool off_position = false;
    std::int64_t aton_status = 0;  // Explicit regional/application bits, 0..255.
    bool raim = false;
    bool virtual_aid = false;
    bool assigned_mode = false;
};

// Special Class A position report. The shared field layout does not make its
// communication state interchangeable with Type 1's raw/SOTDMA state.
struct Type3 {
    Type3(Mmsi identity, ItdmaState19 state)
        : mmsi(std::move(identity)), radio_state(state) {}

    Mmsi mmsi;
    std::int64_t repeat_indicator = 0;
    std::int64_t navigation_status = 15;
    std::int64_t rot_wire = -128;
    std::int64_t sog_tenths_knots = 1023;
    bool position_accuracy = false;
    std::int64_t longitude = kLongitudeUnavailable;
    std::int64_t latitude = kLatitudeUnavailable;
    std::int64_t cog_tenths_degrees = 3600;
    std::int64_t true_heading_degrees = 511;
    std::int64_t utc_second = 60;
    std::int64_t special_manoeuvre = 0;
    bool low_transmit_power = false;
    bool raim = false;
    ItdmaState19 radio_state;
};

// Current-edition Class A static/voyage data. The explicit IMO/flag-state input
// has no guessed unavailable value: current Table 50 omits the meaning of zero.
// Text uses 0x20..0x5F excluding '@'; encode pads shorter text with '@'.
struct Type5 {
    Type5(Mmsi identity, std::int64_t imo_or_flag_state)
        : mmsi(std::move(identity)), imo_or_flag_state_number(imo_or_flag_state) {}

    static constexpr std::int64_t ais_version_indicator = 3;
    Mmsi mmsi;
    std::int64_t repeat_indicator = 0;
    std::int64_t imo_or_flag_state_number;
    std::string call_sign;
    std::string name;
    std::int64_t ship_type = 0;
    std::int64_t dimension_a = 0;
    std::int64_t dimension_b = 0;
    std::int64_t dimension_c = 0;
    std::int64_t dimension_d = 0;
    std::int64_t position_fixing_device = 0;
    std::int64_t eta_month = 0;
    std::int64_t eta_day = 0;
    std::int64_t eta_hour = 24;
    std::int64_t eta_minute = 60;
    std::int64_t draught_tenths_m = 0;
    std::string destination;
    bool dte_not_ready = true;
};

// Table 78's 42-bit identification, not a seven-character vendor string.
// This validates wire syntax, not NMEA code allocation or manufacturer records.
class ManufacturerIdentification {
public:
    ManufacturerIdentification(std::string_view vendor_id,
                               std::int64_t model_code,
                               std::int64_t serial_number) {
        if (vendor_id.size() != 3) {
            throw std::invalid_argument(
                "ManufacturerIdentification.vendor_id: exactly three AIS ASCII characters are required");
        }
        for (const unsigned char character : vendor_id) {
            if (character < 32 || character > 95 || character == '@') {
                throw std::invalid_argument(
                    "ManufacturerIdentification.vendor_id: expected AIS uppercase ASCII 32..95 excluding @");
            }
        }
        if (model_code < 1 || model_code > 15) {
            throw std::invalid_argument(
                "ManufacturerIdentification.model_code: expected 1..15; zero is not used");
        }
        if (serial_number < 0 || serial_number > 0xfffff) {
            throw std::invalid_argument(
                "ManufacturerIdentification.serial_number: expected 0..1048575");
        }
        vendor_id_ = std::string(vendor_id);
        model_code_ = static_cast<std::uint32_t>(model_code);
        serial_number_ = static_cast<std::uint32_t>(serial_number);
    }

    const std::string& vendor_id() const noexcept { return vendor_id_; }
    std::uint32_t model_code() const noexcept { return model_code_; }
    std::uint32_t serial_number() const noexcept { return serial_number_; }

private:
    std::string vendor_id_;
    std::uint32_t model_code_ = 0;
    std::uint32_t serial_number_ = 0;
};

// Class A manufacturer-only Part B in the AIS-only profile. Vessel particulars,
// EPFS and VDES capability are fixed to the source/profile defaults by encode;
// a Class B-style payload cannot be supplied through this type.
struct ClassAType24B {
    ClassAType24B(Mmsi identity, ManufacturerIdentification manufacturer)
        : mmsi(std::move(identity)), manufacturer_id(std::move(manufacturer)) {}

    Mmsi mmsi;
    std::int64_t repeat_indicator = 0;
    ManufacturerIdentification manufacturer_id;
};

// Existing alternative indices 0..4 remain unchanged.
using Message = std::variant<Type1, Type4, Type9, Type18, Type21,
                             Type3, Type5, ClassAType24B>;

}  // namespace aismixer::lab::ais
