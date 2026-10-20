#include "ais_encoder.h"
#include "communication_state.h"
#include "nmea0183.h"

#include <algorithm>
#include <cstdint>
#include <iostream>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <tuple>
#include <type_traits>
#include <utility>
#include <variant>
#include <vector>

// Independent synthetic fixtures, transcribed from M.1371-6 (02/2026),
// Annex 7 Tables 46/47, 50/51, 77/78 and Annex 2 Tables 18/19/20.
// No captured traffic or third-party decoder implementation is reused.
namespace {
using namespace aismixer::lab::ais;
std::map<std::string, unsigned> negatives;
unsigned long long itdma_cases = 0;
unsigned field_checks = 0;

void require(bool value, std::string_view message) {
    if (!value) throw std::runtime_error(std::string(message));
}

template <class F> void invalid(const char* category, F action) {
    try { action(); }
    catch (const std::invalid_argument&) { ++negatives[category]; return; }
    throw std::runtime_error(std::string("accepted invalid ") + category);
}

std::uint64_t field(const std::vector<std::uint8_t>& bits, std::size_t offset,
                    unsigned width) {
    require(offset + width <= bits.size(), "oracle field outside payload");
    std::uint64_t result = 0;
    for (unsigned index = 0; index < width; ++index) {
        require(bits[offset + index] <= 1, "nonbinary encoded bit");
        result = result * 2 + bits[offset + index];
    }
    return result;
}

std::int64_t signed_field(const std::vector<std::uint8_t>& bits,
                          std::size_t offset, unsigned width) {
    const auto value = field(bits, offset, width);
    return (value & (UINT64_C(1) << (width - 1))) != 0
        ? static_cast<std::int64_t>(value) - (INT64_C(1) << width)
        : static_cast<std::int64_t>(value);
}

std::string bit_string(const EncodedPayload& payload) {
    std::string result;
    for (const auto bit : payload.bits) result += static_cast<char>('0' + bit);
    return result;
}

Type3 example3() {
    Type3 result(Mmsi("000000001"),
                ItdmaState19::from_fields(SyncState::BaseDirect, 4097, 6, true));
    result.repeat_indicator = 1;
    result.navigation_status = 5;
    result.rot_wire = -42;
    result.sog_tenths_knots = 123;
    result.position_accuracy = true;
    result.longitude = -73567890;
    result.latitude = -23456789;
    result.cog_tenths_degrees = 1234;
    result.true_heading_degrees = 123;
    result.utc_second = 59;
    result.special_manoeuvre = 2;
    result.low_transmit_power = true;
    result.raim = true;
    return result;
}

Type5 example5() {
    Type5 result(Mmsi("123456789"), 1234567);
    result.repeat_indicator = 2;
    result.call_sign = "LAB1234";
    result.name = "AISMIXER LAB 01";
    result.ship_type = 70;
    result.dimension_a = 100;
    result.dimension_b = 25;
    result.dimension_c = 7;
    result.dimension_d = 9;
    result.position_fixing_device = 15;
    result.eta_month = 2;
    result.eta_day = 29;
    result.eta_hour = 23;
    result.eta_minute = 59;
    result.draught_tenths_m = 123;
    result.destination = "VARNA BG";
    result.dte_not_ready = false;
    return result;
}

ClassAType24B example24b() {
    ClassAType24B result(Mmsi("000123456"),
                        ManufacturerIdentification("ABC", 3, 0x54321));
    result.repeat_indicator = 1;
    return result;
}

template <class T>
void numeric(T message, std::int64_t T::* member, std::size_t offset,
             unsigned width, std::int64_t minimum, std::int64_t maximum,
             const char* category, bool is_signed = false) {
    for (const auto value : {minimum, maximum}) {
        message.*member = value;
        const auto bits = encode(message).bits;
        require(is_signed ? signed_field(bits, offset, width) == value
                          : field(bits, offset, width) == static_cast<std::uint64_t>(value),
                "numeric endpoint at wrong wire offset");
        ++field_checks;
    }
    for (const auto value : {minimum - 1, maximum + 1, INT64_MIN, INT64_MAX}) {
        message.*member = value;
        invalid(category, [&] { (void)encode(message); });
    }
}

template <class T>
void toggle(T message, bool T::* member, std::size_t offset) {
    message.*member = false;
    const auto zero = encode(message).bits;
    message.*member = true;
    const auto one = encode(message).bits;
    require(zero.size() == one.size(), "flag changed payload length");
    for (std::size_t bit = 0; bit < zero.size(); ++bit) {
        require(bit == offset ? zero[bit] == 0 && one[bit] == 1
                              : zero[bit] == one[bit],
                "flag toggle changed unrelated field");
    }
    ++field_checks;
}

template <class T>
void numeric_toggle(T message, std::int64_t T::* member, std::size_t offset,
                    unsigned width, std::int64_t zero = 0) {
    message.*member = zero;
    const auto first = encode(message).bits;
    message.*member = zero + 1;
    const auto second = encode(message).bits;
    for (std::size_t bit = 0; bit < first.size(); ++bit) {
        require(bit == offset + width - 1 ? first[bit] == 0 && second[bit] == 1
                                         : first[bit] == second[bit],
                "numeric one-bit toggle changed unrelated field");
    }
    ++field_checks;
}

template <class T, class = void> struct has_ship_type : std::false_type {};
template <class T> struct has_ship_type<T, std::void_t<decltype(T::ship_type)>>
    : std::true_type {};

template <class T> void only_bit(const T& first, const T& second, std::size_t offset) {
    const auto before=encode(first).bits;
    const auto after=encode(second).bits;
    require(before.size()==after.size(),"one-bit fixture length");
    for(std::size_t bit=0;bit<before.size();++bit)
        require(bit==offset?before[bit]==0 && after[bit]==1:before[bit]==after[bit],
                "one-bit fixture changed unrelated field");
    ++field_checks;
}

void public_contracts() {
    static_assert(!std::is_constructible_v<Type3, Mmsi, SotdmaState19>);
    static_assert(!std::is_constructible_v<Type3, Mmsi, RadioState19>);
    static_assert(!std::is_convertible_v<SotdmaState19, ItdmaState19>);
    static_assert(!std::is_default_constructible_v<Type3>);
    static_assert(!std::is_default_constructible_v<Type5>);
    static_assert(!std::is_default_constructible_v<ClassAType24B>);
    static_assert(!has_ship_type<ClassAType24B>::value);
    static_assert(std::is_same_v<std::variant_alternative_t<0, Message>, Type1>);
    static_assert(std::is_same_v<std::variant_alternative_t<1, Message>, Type4>);
    static_assert(std::is_same_v<std::variant_alternative_t<2, Message>, Type9>);
    static_assert(std::is_same_v<std::variant_alternative_t<3, Message>, Type18>);
    static_assert(std::is_same_v<std::variant_alternative_t<4, Message>, Type21>);
    require(Message(Type1(Mmsi("000000001"), RadioState19(0x7ffff))).index() == 0,
            "legacy variant index changed");
    require(Message(Type4(Mmsi("000000001"), RadioState19(0))).index() == 1,
            "legacy Type4 index changed");
    require(Message(Type9(Mmsi("000000001"), RadioState20(0))).index() == 2,
            "legacy Type9 index changed");
    require(Message(Type18(Mmsi("000000001"), RadioState20(0))).index() == 3,
            "legacy Type18 index changed");
    require(Message(Type21(Mmsi("000000001"))).index() == 4,
            "legacy Type21 index changed");
    for (const auto& value : std::vector<Message>{example3(), example5(), example24b()}) {
        const auto visited = encode(value);
        const auto direct = std::visit([](const auto& item) { return encode(item); }, value);
        require(visited.bits == direct.bits, "new variant visitation changed payload");
    }
    for (const auto raw : {0, 1, 0x7ffff}) {
        require(RadioState19(raw).value() == static_cast<std::uint32_t>(raw),
                "legacy raw state behavior changed");
    }
    for (const auto& value : std::vector<std::string>{"", "000000000", "12345678",
         "1234567890", "12345678A", "+12345678", "-12345678", " 12345678",
         "12345678 ", "12345678\n", "\xef\xbc\x91" "23456789",
         std::string("12345678\0", 9)}) {
        invalid("identity", [&] { (void)Mmsi(value); });
    }
    require(example3().mmsi.digits() == "000000001" &&
            example24b().mmsi.digits() == "000123456", "leading-zero MMSI lost");
}

void type3_fields() {
    Type3 message(Mmsi("000000001"), ItdmaState19::from_fields(SyncState::UtcDirect, 0, 0, false));
    const auto defaults = encode(message).bits;
    require(defaults.size() == 168 && field(defaults, 0, 6) == 3 &&
        field(defaults, 6, 2) == 0 && field(defaults, 8, 30) == 1 &&
        field(defaults, 38, 4) == 15 && signed_field(defaults, 42, 8) == -128 &&
        field(defaults, 50, 10) == 1023 && field(defaults, 60, 1) == 0 &&
        signed_field(defaults, 61, 28) == 108600000 &&
        signed_field(defaults, 89, 27) == 54600000 &&
        field(defaults, 116, 12) == 3600 && field(defaults, 128, 9) == 511 &&
        field(defaults, 137, 6) == 60 && field(defaults, 143, 25) == 0,
        "Type3 default/unavailable/spare fixture");
    numeric(message, &Type3::repeat_indicator, 6, 2, 0, 3, "type3_numeric");
    numeric(message, &Type3::rot_wire, 42, 8, -128, 127, "type3_numeric", true);
    numeric(message, &Type3::sog_tenths_knots, 50, 10, 0, 1023, "type3_numeric");
    numeric(message, &Type3::cog_tenths_degrees, 116, 12, 0, 3600, "type3_numeric");
    numeric(message, &Type3::utc_second, 137, 6, 0, 63, "type3_numeric");
    numeric(message, &Type3::special_manoeuvre, 143, 2, 0, 3, "type3_numeric");
    for (const auto status : {0,1,2,3,4,5,6,7,8,11,12,14,15}) {
        message.navigation_status = status;
        require(field(encode(message).bits,38,4) == static_cast<unsigned>(status), "Type3 status");
    }
    for (const auto status : std::vector<std::int64_t>{-1,9,10,13,16,INT64_MIN,INT64_MAX}) {
        message.navigation_status = status;
        invalid("type3_reserved", [&] { (void)encode(message); });
    }
    message.navigation_status = 15;
    for (const auto heading : {0,359,511}) {
        message.true_heading_degrees = heading;
        require(field(encode(message).bits,128,9) == static_cast<unsigned>(heading), "Type3 heading");
    }
    for (const auto heading : std::vector<std::int64_t>{-1,360,510,512,INT64_MIN,INT64_MAX}) {
        message.true_heading_degrees = heading;
        invalid("type3_reserved", [&] { (void)encode(message); });
    }
    message.true_heading_degrees = 511;
    for (const auto value : {-108000000,-1,0,1,108000000,108600000}) {
        message.longitude = value;
        require(signed_field(encode(message).bits,61,28) == value, "Type3 longitude");
    }
    for (const auto value : std::vector<std::int64_t>{-108000001,108000001,108599999,-108600000,INT64_MIN,INT64_MAX}) {
        message.longitude = value;
        invalid("type3_position", [&] { (void)encode(message); });
    }
    message.longitude = 0;
    for (const auto value : {-54000000,-1,0,1,54000000,54600000}) {
        message.latitude = value;
        require(signed_field(encode(message).bits,89,27) == value, "Type3 latitude");
    }
    for (const auto value : std::vector<std::int64_t>{-54000001,54000001,54599999,-54600000,INT64_MIN,INT64_MAX}) {
        message.latitude = value;
        invalid("type3_position", [&] { (void)encode(message); });
    }
    message.latitude = 0;
    toggle(message,&Type3::position_accuracy,60);
    toggle(message,&Type3::low_transmit_power,147);
    toggle(message,&Type3::raim,148);
    numeric_toggle(message,&Type3::repeat_indicator,6,2);
    numeric_toggle(message,&Type3::navigation_status,38,4);
    numeric_toggle(message,&Type3::rot_wire,42,8);
    numeric_toggle(message,&Type3::sog_tenths_knots,50,10);
    numeric_toggle(message,&Type3::longitude,61,28);
    numeric_toggle(message,&Type3::latitude,89,27);
    numeric_toggle(message,&Type3::cog_tenths_degrees,116,12);
    numeric_toggle(message,&Type3::true_heading_degrees,128,9);
    numeric_toggle(message,&Type3::utc_second,137,6);
    numeric_toggle(message,&Type3::special_manoeuvre,143,2);
    auto next=message;message.mmsi=Mmsi("000000002");next.mmsi=Mmsi("000000003");
    only_bit(message,next,37);
    for(const auto [sync,increment,code,keep,offset] : {
        std::tuple{1,0,0,false,150u},std::tuple{0,1,0,false,163u},
        std::tuple{0,0,1,false,166u},std::tuple{0,0,0,true,167u}}) {
        message.radio_state=ItdmaState19::from_fields(SyncState::UtcDirect,0,0,false);
        next=message;
        next.radio_state=ItdmaState19::from_fields(static_cast<SyncState>(sync),increment,code,keep);
        only_bit(message,next,offset);
    }
}

bool reserved_ship_type(int value) {
    for (const auto reserved : {8,10,15,16,25,26,27,28,47,48,68,87,88,95,96,97,98}) {
        if (value == reserved) return true;
    }
    return value >= 200;
}

void type5_fields() {
    Type5 message(Mmsi("000000001"),1000000);
    const auto defaults = encode(message).bits;
    require(defaults.size() == 424 && field(defaults,0,6) == 5 &&
        field(defaults,6,2) == 0 && field(defaults,8,30) == 1 && field(defaults,38,2) == 3 &&
        field(defaults,40,30) == 1000000 && field(defaults,232,8) == 0 &&
        field(defaults,240,30) == 0 && field(defaults,270,4) == 0 &&
        field(defaults,274,4) == 0 && field(defaults,278,5) == 0 &&
        field(defaults,283,5) == 24 && field(defaults,288,6) == 60 &&
        field(defaults,294,8) == 0 && field(defaults,422,1) == 1 && field(defaults,423,1) == 0,
        "Type5 defaults/edition/spare");
    for (const auto [offset,width] : {std::pair<std::size_t,unsigned>{70,42},{112,120},{302,120}}) {
        for (unsigned bit=0;bit<width;++bit) require(defaults[offset+bit] == 0,"Type5 empty text padding");
    }
    numeric(message,&Type5::repeat_indicator,6,2,0,3,"type5_numeric");
    numeric(message,&Type5::imo_or_flag_state_number,40,30,1000000,1073741823,"type5_identity_number");
    for (const auto value : {0,1,999999}) {
        message.imo_or_flag_state_number=value;
        invalid("type5_identity_number",[&]{(void)encode(message);});
    }
    message.imo_or_flag_state_number=1000000;
    numeric(message,&Type5::dimension_a,240,9,0,511,"type5_numeric");
    numeric(message,&Type5::dimension_b,249,9,0,511,"type5_numeric");
    numeric(message,&Type5::dimension_c,258,6,0,63,"type5_numeric");
    numeric(message,&Type5::dimension_d,264,6,0,63,"type5_numeric");
    numeric(message,&Type5::eta_month,274,4,0,12,"type5_eta");
    numeric(message,&Type5::eta_day,278,5,0,31,"type5_eta");
    numeric(message,&Type5::eta_hour,283,5,0,24,"type5_eta");
    numeric(message,&Type5::eta_minute,288,6,0,60,"type5_eta");
    numeric(message,&Type5::draught_tenths_m,294,8,0,255,"type5_numeric");
    for (int value=-1;value<=256;++value) {
        message.ship_type=value;
        if(value<0 || value>255 || reserved_ship_type(value)) {
            invalid("type5_reserved",[&]{(void)encode(message);});
        } else require(field(encode(message).bits,232,8)==static_cast<unsigned>(value),"Type5 ship type");
    }
    message.ship_type=0;
    for(const auto value : {INT64_MIN,INT64_MAX}) {
        message.ship_type=value;
        invalid("type5_reserved",[&]{(void)encode(message);});
    }
    message.ship_type=0;
    for (const auto value : {0,1,2,3,4,5,6,7,8,9,12,13,14,15}) {
        message.position_fixing_device=value;
        require(field(encode(message).bits,270,4)==static_cast<unsigned>(value),"Type5 EPFS");
    }
    for (const auto value : std::vector<std::int64_t>{-1,10,11,16,INT64_MIN,INT64_MAX}) {
        message.position_fixing_device=value;
        invalid("type5_reserved",[&]{(void)encode(message);});
    }
    message.position_fixing_device=0;
    for (const auto month : {2,4,6,9,11}) {
        message.eta_month=month;message.eta_day=month==2?30:31;
        invalid("type5_eta",[&]{(void)encode(message);});
    }
    message.eta_month=2;message.eta_day=29;(void)encode(message);
    message.eta_month=0;message.eta_day=31;(void)encode(message);
    message.eta_month=12;message.eta_day=0;(void)encode(message);
    message.eta_month=0;message.eta_day=0;
    toggle(message,&Type5::dte_not_ready,422);
    numeric_toggle(message,&Type5::repeat_indicator,6,2);
    numeric_toggle(message,&Type5::imo_or_flag_state_number,40,30,1048576);
    numeric_toggle(message,&Type5::ship_type,232,8);
    numeric_toggle(message,&Type5::dimension_a,240,9);
    numeric_toggle(message,&Type5::dimension_b,249,9);
    numeric_toggle(message,&Type5::dimension_c,258,6);
    numeric_toggle(message,&Type5::dimension_d,264,6);
    numeric_toggle(message,&Type5::position_fixing_device,270,4);
    numeric_toggle(message,&Type5::eta_month,274,4);
    numeric_toggle(message,&Type5::eta_day,278,5);
    numeric_toggle(message,&Type5::eta_hour,283,5);
    numeric_toggle(message,&Type5::eta_minute,288,6);
    numeric_toggle(message,&Type5::draught_tenths_m,294,8);
    auto next=message;message.mmsi=Mmsi("000000002");next.mmsi=Mmsi("000000003");
    only_bit(message,next,37);
    for (const auto [member,characters,offset] : {
        std::tuple{&Type5::call_sign,7u,70u},std::tuple{&Type5::name,20u,112u},
        std::tuple{&Type5::destination,20u,302u}}) {
        message.*member=std::string(characters,'Z');
        const auto maximum=encode(message).bits;
        for(unsigned index=0;index<characters;++index)
            require(field(maximum,offset+index*6,6)==26,"Type5 max text/padding");
        for(unsigned ch=32;ch<=95;++ch) {
            if(ch=='@')continue;
            message.*member=std::string(1,static_cast<char>(ch));
            const auto bits=encode(message).bits;
            require(field(bits,offset,6)==(ch&63),"Type5 ASCII six-bit mapping");
            for(unsigned index=1;index<characters;++index)
                require(field(bits,offset+index*6,6)==0,"Type5 terminator padding");
        }
        for(const auto& value : std::vector<std::string>{"@","A@B","lower","\x1f","\x7f","`","\xc3\xa9",
            std::string("A\0B",3),std::string(characters+1,'A'),std::string(10000,'A')}) {
            message.*member=value;
            invalid("type5_text",[&]{(void)encode(message);});
        }
        message.*member="B";const auto first=encode(message).bits;
        message.*member="C";const auto second=encode(message).bits;
        for(std::size_t bit=0;bit<first.size();++bit)
            require(bit==offset+5?first[bit]==0 && second[bit]==1:first[bit]==second[bit],"text one-bit toggle");
        ++field_checks;message.*member="";
    }
}

void manufacturer_tests() {
    numeric(example24b(),&ClassAType24B::repeat_indicator,6,2,0,3,"manufacturer");
    for(const auto& value : std::vector<std::string>{"","A","AB","ABCD","A@B","abc","A\nB","A`B",
        "A\x7f" "B","\xc3\xa9" "A",std::string("A\0B",3)}) {
        invalid("manufacturer",[&]{(void)ManufacturerIdentification(value,1,0);});
    }
    for(const auto model : std::vector<std::int64_t>{-1,0,16,INT64_MIN,INT64_MAX})
        invalid("manufacturer",[&]{(void)ManufacturerIdentification("ABC",model,0);});
    for(const auto serial : std::vector<std::int64_t>{-1,1048576,INT64_MIN,INT64_MAX})
        invalid("manufacturer",[&]{(void)ManufacturerIdentification("ABC",1,serial);});
    for(const auto model : {1,15}) for(const auto serial : {0,1048575}) {
        const auto bits=encode(ClassAType24B(Mmsi("000000001"),ManufacturerIdentification("ABC",model,serial))).bits;
        require(bits.size()==168 && field(bits,0,6)==24 && field(bits,38,2)==1 &&
            field(bits,40,8)==0 && field(bits,66,4)==static_cast<unsigned>(model) &&
            field(bits,70,20)==static_cast<unsigned>(serial) &&
            field(bits,90,42)==0 && field(bits,132,30)==0 && field(bits,162,4)==0 && field(bits,166,2)==0,
            "Class A 24B restricted/default fields");
    }
    for(unsigned ch=32;ch<=95;++ch) {
        if(ch=='@')continue;
        const auto bits=encode(ClassAType24B(Mmsi("000000001"),ManufacturerIdentification(std::string(3,static_cast<char>(ch)),1,0))).bits;
        for(unsigned index=0;index<3;++index)require(field(bits,48+index*6,6)==(ch&63),"manufacturer six-bit alphabet");
    }
    const auto identity=Mmsi("000000001");
    only_bit(ClassAType24B(identity,ManufacturerIdentification("BBC",2,0)),
             ClassAType24B(identity,ManufacturerIdentification("CBC",2,0)),53);
    only_bit(ClassAType24B(identity,ManufacturerIdentification("ABC",2,0)),
             ClassAType24B(identity,ManufacturerIdentification("ABC",3,0)),69);
    only_bit(ClassAType24B(identity,ManufacturerIdentification("ABC",1,0)),
             ClassAType24B(identity,ManufacturerIdentification("ABC",1,1)),89);
    only_bit(ClassAType24B(Mmsi("000000002"),ManufacturerIdentification("ABC",1,0)),
             ClassAType24B(Mmsi("000000003"),ManufacturerIdentification("ABC",1,0)),37);
    numeric_toggle(example24b(),&ClassAType24B::repeat_indicator,6,2);
    ManufacturerIdentification moved("ABC",1,0);
    const auto destination=std::move(moved);
    require(destination.vendor_id()=="ABC","manufacturer move destination");
    if(moved.vendor_id().size()!=3) {
        const ClassAType24B source(identity,moved);
        invalid("manufacturer_moved_from",[&]{(void)encode(source);});
    } else (void)encode(ClassAType24B(identity,moved));
}

void semantic_states() {
    for(unsigned sync=0;sync<4;++sync) for(unsigned increment=0;increment<8192;++increment)
    for(unsigned code=0;code<8;++code) for(unsigned keep=0;keep<2;++keep) {
        const auto semantic=ItdmaState19::from_fields(static_cast<SyncState>(sync),increment,code,keep!=0);
        const std::uint32_t expected=(sync<<17)|(increment<<4)|(code<<1)|keep;
        const unsigned slots=code<5?code+1:code-4;
        const unsigned effective=increment+(code>=5?8192:0);
        require(semantic.raw().value()==expected && semantic.wire_increment()==increment &&
            semantic.slot_count_code()==code && semantic.slot_count()==slots &&
            semantic.effective_increment()==effective && semantic.keep()==(keep!=0) &&
            semantic.no_further_allocation()==(effective==0) &&
            semantic.sync_state()==static_cast<SyncState>(sync),"ITDMA independent exhaustive wire oracle");
        const auto decoded=ItdmaState19::decode(RadioState19(expected));
        require(decoded.raw().value()==expected && decoded.slot_count()==slots &&
            decoded.effective_increment()==effective,"ITDMA semantic inspection");
        require(ItdmaState19::from_effective(static_cast<SyncState>(sync),effective,slots,keep!=0).raw().value()==expected,
            "ITDMA effective construction");
        ++itdma_cases;
    }
    for(const auto value : std::vector<std::int64_t>{-1,8192,INT64_MIN,INT64_MAX})
        invalid("itdma",[&]{(void)ItdmaState19::from_fields(SyncState::UtcDirect,value,0,false);});
    for(const auto value : std::vector<std::int64_t>{-1,8,INT64_MIN,INT64_MAX})
        invalid("itdma",[&]{(void)ItdmaState19::from_fields(SyncState::UtcDirect,1,value,false);});
    for(const auto value : std::vector<std::int64_t>{-1,16384,INT64_MIN,INT64_MAX})
        invalid("itdma",[&]{(void)ItdmaState19::from_effective(SyncState::UtcDirect,value,1,false);});
    for(const auto value : std::vector<std::int64_t>{-1,0,6,INT64_MIN,INT64_MAX})
        invalid("itdma",[&]{(void)ItdmaState19::from_effective(SyncState::UtcDirect,1,value,false);});
    for(const auto slots : {4,5})
        invalid("itdma",[&]{(void)ItdmaState19::from_effective(SyncState::UtcDirect,8192,slots,false);});
    invalid("itdma",[]{(void)ItdmaState19::from_fields(static_cast<SyncState>(4),1,0,false);});
    invalid("sotdma",[]{(void)SotdmaState19::offset(static_cast<SyncState>(255),0);});
    for(const auto value : std::vector<std::int64_t>{-1,256,INT64_C(1)<<32,INT64_MIN,INT64_MAX}) {
        invalid("sync_state",[&]{(void)ItdmaState19::from_fields(static_cast<SyncState>(value),0,0,false);});
        invalid("sync_state",[&]{(void)ItdmaState19::from_effective(static_cast<SyncState>(value),0,1,false);});
        invalid("sync_state",[&]{(void)SotdmaState19::offset(static_cast<SyncState>(value),0);});
    }
    for(unsigned sync=0;sync<4;++sync) {
        const auto state=static_cast<SyncState>(sync);
        for(const auto offset : {0,1,2249,2250,4499}) {
            const auto value=SotdmaState19::offset(state,offset);
            require(value.raw().value()==(sync<<17)+static_cast<unsigned>(offset) &&
                value.sync_state()==state && value.slot_timeout()==0,"SOTDMA offset bits");
            require(std::get<SotdmaSlotOffset>(SotdmaState19::decode(value.raw()).submessage()).offset==offset,"SOTDMA offset inspection");
        }
        for(const auto hour : {0,23}) for(const auto minute : {0,59}) {
            const auto value=SotdmaState19::utc(state,hour,minute);
            require(value.raw().value()==(sync<<17)+(1u<<14)+(static_cast<unsigned>(hour)<<9)+(static_cast<unsigned>(minute)<<2) &&
                value.sync_state()==state && value.slot_timeout()==1,"SOTDMA UTC bits");
            const auto decoded=std::get<SotdmaUtc>(SotdmaState19::decode(value.raw()).submessage());
            require(decoded.hour==hour && decoded.minute==minute,"SOTDMA UTC inspection");
        }
        for(const auto timeout : {2,4,6}) for(const auto slot : {0,2249}) {
            const auto value=SotdmaState19::current_slot(state,timeout,slot);
            require(value.raw().value()==(sync<<17)+(static_cast<unsigned>(timeout)<<14)+static_cast<unsigned>(slot) &&
                value.sync_state()==state && value.slot_timeout()==static_cast<unsigned>(timeout),"SOTDMA current slot bits");
            require(std::get<SotdmaCurrentSlot>(SotdmaState19::decode(value.raw()).submessage()).slot==slot,"SOTDMA current slot inspection");
        }
        for(const auto timeout : {3,5,7}) for(const auto count : {0,16383}) {
            const auto value=SotdmaState19::received_stations(state,timeout,count);
            require(value.raw().value()==(sync<<17)+(static_cast<unsigned>(timeout)<<14)+static_cast<unsigned>(count) &&
                value.sync_state()==state && value.slot_timeout()==static_cast<unsigned>(timeout),"SOTDMA count bits");
            require(std::get<SotdmaReceivedStations>(SotdmaState19::decode(value.raw()).submessage()).count==count,"SOTDMA count inspection");
        }
    }
    for(const auto value : std::vector<std::int64_t>{-1,16384,INT64_MIN,INT64_MAX}) {
        invalid("sotdma",[&]{(void)SotdmaState19::offset(SyncState::UtcDirect,value);});
        invalid("sotdma",[&]{(void)SotdmaState19::received_stations(SyncState::UtcDirect,3,value);});
    }
    for(const auto value : {4500,16383})
        invalid("sotdma",[&]{(void)SotdmaState19::offset(SyncState::UtcDirect,value);});
    for(const auto [current,next,expected] : {std::tuple{2249,0,1},std::tuple{0,2249,4499},std::tuple{100,200,2350}})
        require(std::get<SotdmaSlotOffset>(SotdmaState19::offset_in_next_frame(SyncState::UtcDirect,current,next).submessage()).offset==expected,"next-frame offset source-derived relation");
    for(const auto value : std::vector<std::int64_t>{-1,2250,INT64_MIN,INT64_MAX}) {
        invalid("sotdma_context",[&]{(void)SotdmaState19::offset_in_next_frame(SyncState::UtcDirect,value,0);});
        invalid("sotdma_context",[&]{(void)SotdmaState19::offset_in_next_frame(SyncState::UtcDirect,0,value);});
    }
    for(const auto value : std::vector<std::int64_t>{-1,2250,16383,INT64_MIN,INT64_MAX})
        invalid("sotdma",[&]{(void)SotdmaState19::current_slot(SyncState::UtcDirect,2,value);});
    for(const auto hour : std::vector<std::int64_t>{-1,24,31,INT64_MIN,INT64_MAX})
        invalid("sotdma_no_utc",[&]{(void)SotdmaState19::utc(SyncState::UtcDirect,hour,0);});
    for(const auto minute : std::vector<std::int64_t>{-1,60,127,INT64_MIN,INT64_MAX})
        invalid("sotdma_no_utc",[&]{(void)SotdmaState19::utc(SyncState::UtcDirect,0,minute);});
    for(const auto timeout : {-1,0,1,3,5,7,8})
        invalid("sotdma_variant",[&]{(void)SotdmaState19::current_slot(SyncState::UtcDirect,timeout,0);});
    for(const auto timeout : {-1,0,1,2,4,6,8})
        invalid("sotdma_variant",[&]{(void)SotdmaState19::received_stations(SyncState::UtcDirect,timeout,0);});
    for(const auto timeout : {INT64_MIN,INT64_MAX})
        invalid("sotdma_variant",[&]{(void)SotdmaState19::from_submessage(SyncState::UtcDirect,timeout,SotdmaSlotOffset{0});});
    invalid("sotdma_no_utc",[]{(void)SotdmaState19::from_submessage(SyncState::UtcDirect,1,SotdmaSlotOffset{0});});
    for(int timeout=0;timeout<=7;++timeout) {
        const std::vector<SotdmaSubmessage> variants={SotdmaSlotOffset{0},SotdmaUtc{0,0},SotdmaCurrentSlot{0},SotdmaReceivedStations{0}};
        const int expected=timeout==0?0:timeout==1?1:timeout%2==0?2:3;
        for(int variant=0;variant<4;++variant) if(variant!=expected)
            invalid("sotdma_variant",[&]{(void)SotdmaState19::from_submessage(SyncState::UtcDirect,timeout,variants[static_cast<std::size_t>(variant)]);});
    }
    for(const auto raw : {0x4001,0x4002,0x4003,0x4100,0x7000,0x40f0,0x88ca,0x8fff,0x1194,0x3fff})
        invalid("sotdma_untrusted_raw",[&]{(void)SotdmaState19::decode(RadioState19(raw));});
}

std::string hex_bytes(std::string_view text) {
    constexpr char hex[]="0123456789ABCDEF";
    std::string result;
    for(const unsigned char ch:text) {result+=hex[ch>>4];result+=hex[ch&15];}
    return result;
}

void vectors() {
    const std::vector<std::pair<std::string,Message>> values={{"type3",example3()},{"type5",example5()},{"classa24b",example24b()}};
    for(const auto& [name,message]:values) {
        const auto payload=encode(message);
        std::cout<<"VECTOR|"<<name<<'|'<<payload.bits.size()<<'|'<<unsigned(payload.fill_bits)<<'|'<<payload.armored<<'|'<<bit_string(payload)<<'\n';
        for(const auto& sentence:format_aivdm(payload,name=="type5"?'B':'A',7))
            std::cout<<"NMEA|"<<name<<'|'<<hex_bytes(sentence)<<'\n';
    }
    for(int code=0;code<8;++code)
        std::cout<<"STATE|itdma"<<code<<'|'<<ItdmaState19::from_fields(SyncState::Other,8191,code,true).raw().value()<<'\n';
    for(int timeout=0;timeout<8;++timeout) {
        const auto state=timeout==0?SotdmaState19::offset(SyncState::BaseDirect,4499):
            timeout==1?SotdmaState19::utc(SyncState::BaseDirect,23,59):
            timeout%2==0?SotdmaState19::current_slot(SyncState::BaseDirect,timeout,2249):
            SotdmaState19::received_stations(SyncState::BaseDirect,timeout,16383);
        std::cout<<"STATE|sotdma"<<timeout<<'|'<<state.raw().value()<<'\n';
    }
}

void literal_goldens() {
    const auto three=encode(example3());
    require(bit_string(three)=="000011010000000000000000000000000000010101110101100001111011110111001110101110001011011101101001101000010011111010110100110100100011110111110111000111010000000000011101","Type3 independent literal bits");
    require(three.armored=="3@0000EmQso>f;Ma\x60" "CrllSoo7@0M" && three.fill_bits==0,"Type3 golden armor");
    const auto five=encode(example5());
    require(bit_string(five)=="0001011000011101011011110011010001010111000000000100101101011010000111001100000001000010110001110010110011110100000001001001010011001101001001011000000101010010100000001100000001000010100000110000110001000000000000000000000000000000010001100011001000000110010001110010011111001011101101111110110111101101011000000101001000111000000110000000001000011100000000000000000000000000000000000000000000000000000000000000000000000000","Type5 independent literal bits");
    require(five.armored=="5Qmg=5L0Bm\x60" "Lh4;7;?@4U<lUPE:0h4:334000016<PI79tfosNmPDSPH0Qh000000000000" && five.fill_bits==2,"Type5 golden armor");
    const auto partb=encode(example24b());
    require(bit_string(partb)=="011000010000000000000111100010010000000100000000000001000010000011001101010100001100100001000000000000000000000000000000000000000000000000000000000000000000000000000000","Class A24B independent literal bits");
    require(partb.armored=="H@07R@40123=D<Q0000000000000" && partb.fill_bits==0,"Class A24B golden armor");
}

void nmea_negatives() {
    const auto payload=encode(example5());
    invalid("nmea",[&]{(void)format_aivdm(payload,'A');});
    invalid("nmea",[&]{(void)format_aivdm(payload,'A',10);});
    invalid("nmea",[&]{(void)format_aivdm(payload,'C',7);});
    auto good=format_aivdm(payload,'B',7);
    require(good.size()==2 && good[0].size()==82 && good[1].size()==33,"Type5 bounded multipart dimensions");
    std::ostringstream positive;write_sentences(positive,good);
    require(positive.str()==good[0]+good[1],"ordered complete message rendering");
    auto reject=[&](std::vector<std::string> sentences) {
        std::ostringstream output;
        invalid("nmea",[&]{write_sentences(output,sentences);});
        require(output.str().empty(),"invalid multipart wrote partial output");
    };
    reject({});reject({good[0]});reject({good[1]});reject({good[1],good[0]});
    reject({good[0],good[0]});reject({good[0],good[1],good[1]});
    auto altered=good;altered[0][20]^=1;reject(altered);
    altered=good;altered[1].pop_back();reject(altered);
    auto malformed=payload;malformed.bits.pop_back();
    invalid("nmea",[&]{(void)format_aivdm(malformed,'B',7);});
}
} // namespace

int main(int argc,char** argv) {
    try {
        if(argc==2 && std::string_view(argv[1])=="--vectors") {vectors();return 0;}
        if(argc!=1)throw std::invalid_argument("use no argument or --vectors");
        public_contracts();literal_goldens();type3_fields();type5_fields();
        manufacturer_tests();semantic_states();nmea_negatives();
        unsigned total=0;
        for(const auto& [category,count]:negatives) {
            std::cout<<"NEGATIVE "<<category<<'='<<count<<'\n';total+=count;
        }
        std::cout<<"PASS Class A payload/state: exact_field_checks="<<field_checks
                 <<" exhaustive_itdma_wire_cases="<<itdma_cases<<" rejected="<<total<<'\n';
        return 0;
    }catch(const std::exception& error) {
        std::cerr<<"FAIL Class A payload/state: "<<error.what()<<'\n';return 1;
    }
}
