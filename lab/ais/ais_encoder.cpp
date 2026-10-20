#include "ais_encoder.h"

#include "ais_bits.h"

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>

namespace aismixer::lab::ais {
namespace {

// Protocol facts were checked against ITU-R M.1371-6 (02/2026), Annex 7,
// Tables 46, 49, 57, 68 and 71. No native object representation is serialized.
void require_range(std::int64_t value, std::int64_t minimum,
                   std::int64_t maximum, std::string_view field) {
    if (value < minimum || value > maximum) {
        throw std::invalid_argument(std::string(field) + ": expected " +
                                    std::to_string(minimum) + ".." +
                                    std::to_string(maximum) + ", got " +
                                    std::to_string(value));
    }
}

void validate_position(std::int64_t longitude, std::int64_t latitude,
                       std::string_view prefix) {
    if (longitude != 108600000) {
        require_range(longitude, -108000000, 108000000,
                      std::string(prefix) + ".longitude");
    }
    if (latitude != 54600000) {
        require_range(latitude, -54000000, 54000000,
                      std::string(prefix) + ".latitude");
    }
}

void validate_heading(std::int64_t heading, std::string_view field) {
    if (heading != 511) {
        require_range(heading, 0, 359, field);
    }
}

void validate_epfs(std::int64_t epfs, std::string_view field) {
    require_range(epfs, 0, 15, field);
    if (epfs == 10 || epfs == 11) {
        throw std::invalid_argument(std::string(field) +
                                    ": values 10 and 11 are reserved");
    }
}

void validate_date(const Type4& message) {
    require_range(message.utc_year, 0, 9999, "Type4.utc_year");
    require_range(message.utc_month, 0, 12, "Type4.utc_month");
    require_range(message.utc_day, 0, 31, "Type4.utc_day");
    require_range(message.utc_hour, 0, 24, "Type4.utc_hour");
    require_range(message.utc_minute, 0, 60, "Type4.utc_minute");
    require_range(message.utc_second, 0, 60, "Type4.utc_second");
    // Each date component has its own unavailable marker. Validate a known
    // month/day even if the year is unavailable; February 29 then remains legal.
    if (message.utc_month != 0 && message.utc_day != 0) {
        constexpr int days_by_month[] = {0, 31, 29, 31, 30, 31, 30,
                                        31, 31, 30, 31, 30, 31};
        int limit = days_by_month[static_cast<std::size_t>(message.utc_month)];
        const auto year = message.utc_year;
        if (message.utc_month == 2 && year != 0 &&
            !(year % 4 == 0 && (year % 100 != 0 || year % 400 == 0))) {
            limit = 28;
        }
        if (message.utc_day > limit) {
            throw std::invalid_argument(
                "Type4.utc_day: impossible date for supplied UTC month/year");
        }
    }
}

void append_number(BitWriter& writer, std::int64_t value, unsigned width,
                   std::string_view field) {
    // All unsigned model fields are validated before the first bit is written.
    writer.append_unsigned(static_cast<std::uint64_t>(value), width, field);
}

void append_flag(BitWriter& writer, bool value, std::string_view field) {
    writer.append_unsigned(value ? 1u : 0u, 1, field);
}

void append_header(BitWriter& writer, std::uint64_t type,
                   std::int64_t repeat_indicator, const Mmsi& mmsi) {
    writer.append_unsigned(type, 6, "message_type");
    append_number(writer, repeat_indicator, 2, "repeat_indicator");
    writer.append_unsigned(mmsi.wire_value(), 30, "mmsi");
}

EncodedPayload finish(const BitWriter& writer, std::size_t expected_bits) {
    if (writer.bits().size() != expected_bits) {
        throw std::logic_error("AIS encoder: incorrect internal payload length");
    }
    return armor_bits(writer.bits());
}

void validate_name(const std::string& name) {
    if (name.size() > 20) {
        throw std::invalid_argument("Type21.name: maximum length is 20 ASCII characters");
    }
    for (const unsigned char character : name) {
        if (character < 32 || character > 95 || character == '@') {
            throw std::invalid_argument(
                "Type21.name: expected AIS uppercase ASCII 32..95 excluding @ terminator");
        }
    }
}

void validate_text(const std::string& text, std::size_t maximum,
                   std::string_view field) {
    if (text.size() > maximum) {
        throw std::invalid_argument(std::string(field) + ": maximum length is " +
                                    std::to_string(maximum) + " ASCII characters");
    }
    for (const unsigned char character : text) {
        if (character < 32 || character > 95 || character == '@') {
            throw std::invalid_argument(std::string(field) +
                ": expected AIS uppercase ASCII 32..95 excluding @ terminator");
        }
    }
}

void append_text(BitWriter& writer, const std::string& text,
                 std::size_t characters, std::string_view field) {
    for (std::size_t index = 0; index < characters; ++index) {
        const unsigned char character = index < text.size()
            ? static_cast<unsigned char>(text[index]) : static_cast<unsigned char>('@');
        writer.append_unsigned(character >= 64 ? character - 64u : character, 6, field);
    }
}

void validate_ship_type(std::int64_t ship_type) {
    // Table 50 permits explicit regional codes 100..199; their assignment is
    // not verified here. Table 51's future-reserved codes remain prohibited.
    require_range(ship_type, 0, 199, "Type5.ship_type");
    switch (ship_type) {
    case 8: case 10: case 15: case 16: case 25: case 26: case 27: case 28:
    case 47: case 48: case 68: case 87: case 88: case 95: case 96: case 97: case 98:
        throw std::invalid_argument("Type5.ship_type: value is reserved for future use");
    default:
        break;
    }
}

void validate_eta(const Type5& message) {
    require_range(message.eta_month, 0, 12, "Type5.eta_month");
    require_range(message.eta_day, 0, 31, "Type5.eta_day");
    require_range(message.eta_hour, 0, 24, "Type5.eta_hour");
    require_range(message.eta_minute, 0, 60, "Type5.eta_minute");
    // ETA has no year. February 29 is representable; each unavailable component
    // remains independent, as in Table 50's component-specific zero markers.
    if (message.eta_month != 0 && message.eta_day != 0) {
        constexpr int days_by_month[] = {0, 31, 29, 31, 30, 31, 30,
                                        31, 31, 30, 31, 30, 31};
        if (message.eta_day > days_by_month[static_cast<std::size_t>(message.eta_month)]) {
            throw std::invalid_argument("Type5.eta_day: impossible day for supplied ETA month");
        }
    }
}

}  // namespace

EncodedPayload armor_bits(const std::vector<std::uint8_t>& bits) {
    if (bits.empty() || bits.size() > kMaxPayloadBits) {
        throw std::invalid_argument("AIS bits: expected a nonempty vector of at most " +
                                    std::to_string(kMaxPayloadBits) + " bits");
    }
    for (const auto bit : bits) {
        if (bit > 1) {
            throw std::invalid_argument("AIS bits: each bit must be 0 or 1");
        }
    }
    EncodedPayload result{bits, {}, static_cast<std::uint8_t>((6 - bits.size() % 6) % 6)};
    result.armored.reserve((bits.size() + 5) / 6);
    for (std::size_t offset = 0; offset < bits.size(); offset += 6) {
        unsigned value = 0;
        for (std::size_t bit = 0; bit < 6; ++bit) {
            value = (value << 1) |
                    (offset + bit < bits.size() ? bits[offset + bit] : 0u);
        }
        result.armored.push_back(static_cast<char>(value + 48 + (value >= 40 ? 8 : 0)));
    }
    return result;
}

EncodedPayload encode(const Type1& message) {
    require_range(message.repeat_indicator, 0, 3, "Type1.repeat_indicator");
    require_range(message.navigation_status, 0, 15, "Type1.navigation_status");
    if (message.navigation_status == 9 || message.navigation_status == 10 ||
        message.navigation_status == 13) {
        throw std::invalid_argument("Type1.navigation_status: value is reserved for future use");
    }
    require_range(message.rot_wire, -128, 127, "Type1.rot_wire");
    require_range(message.sog_tenths_knots, 0, 1023, "Type1.sog_tenths_knots");
    validate_position(message.longitude, message.latitude, "Type1");
    require_range(message.cog_tenths_degrees, 0, 3600, "Type1.cog_tenths_degrees");
    validate_heading(message.true_heading_degrees, "Type1.true_heading_degrees");
    require_range(message.utc_second, 0, 63, "Type1.utc_second");
    require_range(message.special_manoeuvre, 0, 3, "Type1.special_manoeuvre");

    BitWriter writer;
    append_header(writer, 1, message.repeat_indicator, message.mmsi);
    append_number(writer, message.navigation_status, 4, "Type1.navigation_status");
    writer.append_signed(message.rot_wire, 8, "Type1.rot_wire");
    append_number(writer, message.sog_tenths_knots, 10, "Type1.sog_tenths_knots");
    append_flag(writer, message.position_accuracy, "Type1.position_accuracy");
    writer.append_signed(message.longitude, 28, "Type1.longitude");
    writer.append_signed(message.latitude, 27, "Type1.latitude");
    append_number(writer, message.cog_tenths_degrees, 12, "Type1.cog_tenths_degrees");
    append_number(writer, message.true_heading_degrees, 9, "Type1.true_heading_degrees");
    append_number(writer, message.utc_second, 6, "Type1.utc_second");
    append_number(writer, message.special_manoeuvre, 2, "Type1.special_manoeuvre");
    writer.append_unsigned(0, 2, "Type1.spare");
    append_flag(writer, message.low_transmit_power, "Type1.low_transmit_power");
    append_flag(writer, message.raim, "Type1.raim");
    writer.append_unsigned(message.radio_state.value(), 19, "Type1.radio_state");
    return finish(writer, 168);
}

EncodedPayload encode(const Type4& message) {
    require_range(message.repeat_indicator, 0, 3, "Type4.repeat_indicator");
    validate_date(message);
    validate_position(message.longitude, message.latitude, "Type4");
    validate_epfs(message.position_fixing_device, "Type4.position_fixing_device");

    BitWriter writer;
    append_header(writer, 4, message.repeat_indicator, message.mmsi);
    append_number(writer, message.utc_year, 14, "Type4.utc_year");
    append_number(writer, message.utc_month, 4, "Type4.utc_month");
    append_number(writer, message.utc_day, 5, "Type4.utc_day");
    append_number(writer, message.utc_hour, 5, "Type4.utc_hour");
    append_number(writer, message.utc_minute, 6, "Type4.utc_minute");
    append_number(writer, message.utc_second, 6, "Type4.utc_second");
    append_flag(writer, message.position_accuracy, "Type4.position_accuracy");
    writer.append_signed(message.longitude, 28, "Type4.longitude");
    writer.append_signed(message.latitude, 27, "Type4.latitude");
    append_number(writer, message.position_fixing_device, 4, "Type4.position_fixing_device");
    append_flag(writer, message.satellite_transmission_control,
                "Type4.satellite_transmission_control");
    writer.append_unsigned(0, 9, "Type4.spare");
    append_flag(writer, message.raim, "Type4.raim");
    writer.append_unsigned(message.radio_state.value(), 19, "Type4.radio_state");
    return finish(writer, 168);
}

EncodedPayload encode(const Type9& message) {
    require_range(message.repeat_indicator, 0, 3, "Type9.repeat_indicator");
    require_range(message.altitude_m, 0, 4095, "Type9.altitude_m");
    require_range(message.sog_knots, 0, 1023, "Type9.sog_knots");
    validate_position(message.longitude, message.latitude, "Type9");
    require_range(message.cog_tenths_degrees, 0, 3600, "Type9.cog_tenths_degrees");
    require_range(message.utc_second, 0, 63, "Type9.utc_second");

    BitWriter writer;
    append_header(writer, 9, message.repeat_indicator, message.mmsi);
    append_number(writer, message.altitude_m, 12, "Type9.altitude_m");
    append_number(writer, message.sog_knots, 10, "Type9.sog_knots");
    append_flag(writer, message.position_accuracy, "Type9.position_accuracy");
    writer.append_signed(message.longitude, 28, "Type9.longitude");
    writer.append_signed(message.latitude, 27, "Type9.latitude");
    append_number(writer, message.cog_tenths_degrees, 12, "Type9.cog_tenths_degrees");
    append_number(writer, message.utc_second, 6, "Type9.utc_second");
    append_flag(writer, message.altitude_barometric, "Type9.altitude_barometric");
    writer.append_unsigned(0, 7, "Type9.spare_before_dte");
    append_flag(writer, message.dte_not_ready, "Type9.dte_not_ready");
    writer.append_unsigned(0, 3, "Type9.spare_after_dte");
    append_flag(writer, message.assigned_mode, "Type9.assigned_mode");
    append_flag(writer, message.raim, "Type9.raim");
    writer.append_unsigned(message.radio_state.value(), 20, "Type9.radio_state");
    return finish(writer, 168);
}

EncodedPayload encode(const Type18& message) {
    require_range(message.repeat_indicator, 0, 3, "Type18.repeat_indicator");
    require_range(message.sog_tenths_knots, 0, 1023, "Type18.sog_tenths_knots");
    validate_position(message.longitude, message.latitude, "Type18");
    require_range(message.cog_tenths_degrees, 0, 3600, "Type18.cog_tenths_degrees");
    validate_heading(message.true_heading_degrees, "Type18.true_heading_degrees");
    require_range(message.utc_second, 0, message.cs_unit ? 60 : 63, "Type18.utc_second");
    if (message.cs_unit) {
        if (message.repeat_indicator != 0) {
            throw std::invalid_argument("Type18.repeat_indicator: Class B CS requires 0");
        }
        if (message.radio_state.value() != 0xE0006u) {
            throw std::invalid_argument(
                "Type18.radio_state: Class B CS requires selector 1 and fixed state 1100000000000000110");
        }
    }

    BitWriter writer;
    append_header(writer, 18, message.repeat_indicator, message.mmsi);
    writer.append_unsigned(0, 8, "Type18.spare_before_sog");
    append_number(writer, message.sog_tenths_knots, 10, "Type18.sog_tenths_knots");
    append_flag(writer, message.position_accuracy, "Type18.position_accuracy");
    writer.append_signed(message.longitude, 28, "Type18.longitude");
    writer.append_signed(message.latitude, 27, "Type18.latitude");
    append_number(writer, message.cog_tenths_degrees, 12, "Type18.cog_tenths_degrees");
    append_number(writer, message.true_heading_degrees, 9, "Type18.true_heading_degrees");
    append_number(writer, message.utc_second, 6, "Type18.utc_second");
    append_flag(writer, message.low_transmit_power, "Type18.low_transmit_power");
    writer.append_unsigned(0, 1, "Type18.spare_after_power");
    append_flag(writer, message.cs_unit, "Type18.cs_unit");
    append_flag(writer, message.display, "Type18.display");
    append_flag(writer, message.dsc, "Type18.dsc");
    append_flag(writer, message.whole_band, "Type18.whole_band");
    append_flag(writer, message.message22, "Type18.message22");
    append_flag(writer, message.assigned_mode, "Type18.assigned_mode");
    append_flag(writer, message.raim, "Type18.raim");
    writer.append_unsigned(message.radio_state.value(), 20, "Type18.radio_state");
    return finish(writer, 168);
}

EncodedPayload encode(const Type21& message) {
    require_range(message.repeat_indicator, 0, 3, "Type21.repeat_indicator");
    require_range(message.aid_type, 0, 31, "Type21.aid_type");
    validate_name(message.name);
    validate_position(message.longitude, message.latitude, "Type21");
    require_range(message.dimension_a, 0, 511, "Type21.dimension_a");
    require_range(message.dimension_b, 0, 511, "Type21.dimension_b");
    require_range(message.dimension_c, 0, 63, "Type21.dimension_c");
    require_range(message.dimension_d, 0, 63, "Type21.dimension_d");
    validate_epfs(message.position_fixing_device, "Type21.position_fixing_device");
    require_range(message.utc_second, 0, 63, "Type21.utc_second");
    require_range(message.aton_status, 0, 255, "Type21.aton_status");
    if ((message.virtual_aid || message.aid_type == 1) &&
        (message.dimension_a != 0 || message.dimension_b != 0 ||
         message.dimension_c != 0 || message.dimension_d != 0)) {
        throw std::invalid_argument(
            "Type21.dimensions: virtual aids and reference points require A=B=C=D=0");
    }

    BitWriter writer;
    append_header(writer, 21, message.repeat_indicator, message.mmsi);
    append_number(writer, message.aid_type, 5, "Type21.aid_type");
    for (std::size_t index = 0; index < 20; ++index) {
        const unsigned char character = index < message.name.size()
                                            ? static_cast<unsigned char>(message.name[index])
                                            : static_cast<unsigned char>('@');
        // AIS text is a different mapping from NMEA payload armoring.
        writer.append_unsigned(character >= 64 ? character - 64u : character,
                               6, "Type21.name");
    }
    append_flag(writer, message.position_accuracy, "Type21.position_accuracy");
    writer.append_signed(message.longitude, 28, "Type21.longitude");
    writer.append_signed(message.latitude, 27, "Type21.latitude");
    append_number(writer, message.dimension_a, 9, "Type21.dimension_a");
    append_number(writer, message.dimension_b, 9, "Type21.dimension_b");
    append_number(writer, message.dimension_c, 6, "Type21.dimension_c");
    append_number(writer, message.dimension_d, 6, "Type21.dimension_d");
    append_number(writer, message.position_fixing_device, 4, "Type21.position_fixing_device");
    append_number(writer, message.utc_second, 6, "Type21.utc_second");
    append_flag(writer, message.off_position, "Type21.off_position");
    append_number(writer, message.aton_status, 8, "Type21.aton_status");
    append_flag(writer, message.raim, "Type21.raim");
    append_flag(writer, message.virtual_aid, "Type21.virtual_aid");
    append_flag(writer, message.assigned_mode, "Type21.assigned_mode");
    writer.append_unsigned(0, 1, "Type21.spare");
    return finish(writer, 272);
}

// M.1371-6 Annex 7 Tables 46/47 (printed 105..107) distinguish Type 3's
// ITDMA state even though its positioning fields share Type 1's wire offsets.
EncodedPayload encode(const Type3& message) {
    require_range(message.repeat_indicator, 0, 3, "Type3.repeat_indicator");
    require_range(message.navigation_status, 0, 15, "Type3.navigation_status");
    if (message.navigation_status == 9 || message.navigation_status == 10 ||
        message.navigation_status == 13) {
        throw std::invalid_argument("Type3.navigation_status: value is reserved for future use");
    }
    require_range(message.rot_wire, -128, 127, "Type3.rot_wire");
    require_range(message.sog_tenths_knots, 0, 1023, "Type3.sog_tenths_knots");
    validate_position(message.longitude, message.latitude, "Type3");
    require_range(message.cog_tenths_degrees, 0, 3600, "Type3.cog_tenths_degrees");
    validate_heading(message.true_heading_degrees, "Type3.true_heading_degrees");
    require_range(message.utc_second, 0, 63, "Type3.utc_second");
    require_range(message.special_manoeuvre, 0, 3, "Type3.special_manoeuvre");

    BitWriter writer;
    append_header(writer, 3, message.repeat_indicator, message.mmsi);
    append_number(writer, message.navigation_status, 4, "Type3.navigation_status");
    writer.append_signed(message.rot_wire, 8, "Type3.rot_wire");
    append_number(writer, message.sog_tenths_knots, 10, "Type3.sog_tenths_knots");
    append_flag(writer, message.position_accuracy, "Type3.position_accuracy");
    writer.append_signed(message.longitude, 28, "Type3.longitude");
    writer.append_signed(message.latitude, 27, "Type3.latitude");
    append_number(writer, message.cog_tenths_degrees, 12, "Type3.cog_tenths_degrees");
    append_number(writer, message.true_heading_degrees, 9, "Type3.true_heading_degrees");
    append_number(writer, message.utc_second, 6, "Type3.utc_second");
    append_number(writer, message.special_manoeuvre, 2, "Type3.special_manoeuvre");
    writer.append_unsigned(0, 2, "Type3.spare");
    append_flag(writer, message.low_transmit_power, "Type3.low_transmit_power");
    append_flag(writer, message.raim, "Type3.raim");
    writer.append_unsigned(message.radio_state.raw().value(), 19, "Type3.radio_state");
    return finish(writer, 168);
}

// Table 50 (printed 109..110), Table 51 (111..114), Figure 38 (115).
EncodedPayload encode(const Type5& message) {
    require_range(message.repeat_indicator, 0, 3, "Type5.repeat_indicator");
    require_range(message.imo_or_flag_state_number, 1000000, 0x3fffffff,
                  "Type5.imo_or_flag_state_number");
    validate_text(message.call_sign, 7, "Type5.call_sign");
    validate_text(message.name, 20, "Type5.name");
    validate_ship_type(message.ship_type);
    require_range(message.dimension_a, 0, 511, "Type5.dimension_a");
    require_range(message.dimension_b, 0, 511, "Type5.dimension_b");
    require_range(message.dimension_c, 0, 63, "Type5.dimension_c");
    require_range(message.dimension_d, 0, 63, "Type5.dimension_d");
    validate_epfs(message.position_fixing_device, "Type5.position_fixing_device");
    validate_eta(message);
    require_range(message.draught_tenths_m, 0, 255, "Type5.draught_tenths_m");
    validate_text(message.destination, 20, "Type5.destination");

    BitWriter writer;
    append_header(writer, 5, message.repeat_indicator, message.mmsi);
    append_number(writer, Type5::ais_version_indicator, 2, "Type5.ais_version_indicator");
    append_number(writer, message.imo_or_flag_state_number, 30, "Type5.imo_or_flag_state_number");
    append_text(writer, message.call_sign, 7, "Type5.call_sign");
    append_text(writer, message.name, 20, "Type5.name");
    append_number(writer, message.ship_type, 8, "Type5.ship_type");
    append_number(writer, message.dimension_a, 9, "Type5.dimension_a");
    append_number(writer, message.dimension_b, 9, "Type5.dimension_b");
    append_number(writer, message.dimension_c, 6, "Type5.dimension_c");
    append_number(writer, message.dimension_d, 6, "Type5.dimension_d");
    append_number(writer, message.position_fixing_device, 4, "Type5.position_fixing_device");
    append_number(writer, message.eta_month, 4, "Type5.eta_month");
    append_number(writer, message.eta_day, 5, "Type5.eta_day");
    append_number(writer, message.eta_hour, 5, "Type5.eta_hour");
    append_number(writer, message.eta_minute, 6, "Type5.eta_minute");
    append_number(writer, message.draught_tenths_m, 8, "Type5.draught_tenths_m");
    append_text(writer, message.destination, 20, "Type5.destination");
    append_flag(writer, message.dte_not_ready, "Type5.dte_not_ready");
    writer.append_unsigned(0, 1, "Type5.spare");
    return finish(writer, 424);
}

// Class A restriction: A7-3.22, printed 140; Table 77, 140..141; Table 78, 142.
EncodedPayload encode(const ClassAType24B& message) {
    require_range(message.repeat_indicator, 0, 3, "ClassAType24B.repeat_indicator");
    // A moved-from identification remains a valid C++ object, but its string
    // need not retain three characters. Recheck before serializing any bits.
    if (message.manufacturer_id.vendor_id().size() != 3) {
        throw std::invalid_argument(
            "ClassAType24B.vendor_id: exactly three AIS ASCII characters are required");
    }
    validate_text(message.manufacturer_id.vendor_id(), 3, "ClassAType24B.vendor_id");
    require_range(message.manufacturer_id.model_code(), 1, 15, "ClassAType24B.model_code");
    require_range(message.manufacturer_id.serial_number(), 0, 0xfffff,
                  "ClassAType24B.serial_number");

    BitWriter writer;
    append_header(writer, 24, message.repeat_indicator, message.mmsi);
    writer.append_unsigned(1, 2, "ClassAType24B.part_number");
    writer.append_unsigned(0, 8, "ClassAType24B.ship_type_unavailable");
    append_text(writer, message.manufacturer_id.vendor_id(), 3, "ClassAType24B.vendor_id");
    writer.append_unsigned(message.manufacturer_id.model_code(), 4, "ClassAType24B.model_code");
    writer.append_unsigned(message.manufacturer_id.serial_number(), 20, "ClassAType24B.serial_number");
    writer.append_unsigned(0, 42, "ClassAType24B.call_sign_unavailable");
    writer.append_unsigned(0, 30, "ClassAType24B.dimensions_unavailable");
    writer.append_unsigned(0, 4, "ClassAType24B.epfs_unavailable");
    writer.append_unsigned(0, 2, "ClassAType24B.vdes_ais_only");
    return finish(writer, 168);
}

EncodedPayload encode(const Message& message) {
    return std::visit([](const auto& typed) { return encode(typed); }, message);
}

}  // namespace aismixer::lab::ais
