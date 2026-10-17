#include "rf_contract.h"
#include <algorithm>
#include <iostream>
#include <stdexcept>
#include <string_view>
#include <type_traits>

namespace r = aismixer::lab::runtime;
namespace {
void require(bool value, const char* reason) { if (!value) throw std::runtime_error(reason); }
template<class F> void invalid(F action, std::string_view diagnostic = {}) {
    try { action(); } catch (const std::invalid_argument& error) {
        require(std::string_view(error.what()).find(diagnostic) != std::string_view::npos,
                "RF interface rejected through an unexpected validation branch");
        return;
    }
    throw std::runtime_error("invalid RF interface contract accepted");
}
}
int main() {
    try {
        static_assert(!std::is_convertible_v<r::ReceiverId, r::TransmissionId>);
        static_assert(!std::is_convertible_v<r::ReceptionId, r::TransmissionId>);
        r::RfWorkSubmission work;
        work.transmissions.push_back({{"tx:A:1"}, "station:A", 0, {0}, r::from_seconds(1), "payload:1"});
        work.active_receivers = {{"receiver:R1"}, {"receiver:R2"}};
        work.receiver_changes.push_back({{"receiver:R1"}, true, 28000000000LL, 43000000000LL,
                                         25000, "WGS84_ellipsoid"});
        r::validate_rf_submission(work);
        r::CompletedRfObservation carrier{{"rf:model", 1}, {"tx:A:1"}, {"receiver:R2"},
            std::nullopt, r::from_seconds(0), r::DecodeDisposition::Undecoded, std::nullopt, "station:A"};
        r::CompletedRfObservation decoded{{"rf:model", 2}, {"tx:A:1"}, {"receiver:R1"},
            r::ReceptionId{"reception:T1:R1"}, r::from_seconds(1), r::DecodeDisposition::Decoded,
            "payload:1", std::nullopt};
        r::validate_rf_completion(work, {carrier, decoded});
        auto second_receiver = decoded;
        second_receiver.observation_id.sequence = 3;
        second_receiver.receiver_id = {"receiver:R2"};
        second_receiver.reception_id = r::ReceptionId{"reception:T1:R2"};
        r::validate_rf_completion(work, {carrier, decoded, second_receiver});
        auto failed_second = second_receiver;
        failed_second.disposition = r::DecodeDisposition::Failed;
        failed_second.decoded_payload_reference.reset();
        r::validate_rf_completion(work, {decoded, failed_second});
        // Several transient undecoded observations are allowed; exactly one
        // final decoded/failed result is allowed per physical TX and receiver.
        auto repeated_carrier = carrier;
        repeated_carrier.observation_id.sequence = 4;
        r::validate_rf_completion(work, {carrier, repeated_carrier, second_receiver});
        invalid([&] {
            auto duplicate = decoded;
            duplicate.observation_id.sequence = 5;
            duplicate.reception_id = r::ReceptionId{"reception:another-result"};
            r::validate_rf_completion(work, {decoded, duplicate});
        }, "one TX/receiver pair");
        invalid([&] {
            auto contradiction = decoded;
            contradiction.observation_id.sequence = 5;
            contradiction.reception_id = r::ReceptionId{"reception:contradictory-result"};
            contradiction.disposition = r::DecodeDisposition::Failed;
            contradiction.decoded_payload_reference.reset();
            r::validate_rf_completion(work, {decoded, contradiction});
        }, "one TX/receiver pair");
        require(work.receiver_changes.size() == 1 && work.active_receivers.size() == 2,
                "unchanged resident receiver should not require another H2D site update");
        invalid([&] { auto w=work; w.transmissions[0].end={-1}; r::validate_rf_submission(w); });
        invalid([&] { auto w=work; w.transmissions[0].end=w.transmissions[0].start; r::validate_rf_submission(w); }, "interval");
        auto sequential = work;
        sequential.transmissions.push_back({{"tx:A:2"}, "station:A", 1, r::from_seconds(1), r::from_seconds(2), "payload:2"});
        r::validate_rf_submission(sequential);  // Touching half-open TX intervals.
        std::reverse(sequential.transmissions.begin(), sequential.transmissions.end());
        r::validate_rf_submission(sequential);  // Submission order is immaterial.
        invalid([&] {
            auto overlap = work;
            overlap.transmissions.push_back({{"tx:A:2"}, "station:A", 1, {1}, r::from_seconds(2), "payload:2"});
            r::validate_rf_submission(overlap);
        }, "overlapping physical TX");
        auto independent = work;
        independent.transmissions.push_back({{"tx:B:1"}, "station:B", 1, {0}, r::from_seconds(1), "payload:B"});
        r::validate_rf_submission(independent);  // Simultaneous independent TX.
        invalid([&] { auto w=work; w.commit_watermark={1}; r::validate_rf_submission(w); });
        invalid([&] { auto w=work; w.transmissions.push_back(w.transmissions[0]); r::validate_rf_submission(w); });
        invalid([&] { auto w=work; w.active_receivers.push_back(w.active_receivers[0]); r::validate_rf_submission(w); });
        invalid([&] { auto w=work; w.receiver_changes[0].height_datum="terrain"; r::validate_rf_submission(w); });
        invalid([&] { auto w=work; w.receiver_changes[0].latitude_ndeg=90000000001LL; r::validate_rf_submission(w); });
        invalid([&] { auto w=work; w.receiver_changes[0].enabled=false; r::validate_rf_submission(w); });
        invalid([&] { auto x=decoded; x.availability_time={-1}; r::validate_rf_completion(work,{x}); });
        invalid([&] { auto x=decoded; x.availability_time={1}; r::validate_rf_completion(work,{x}); });
        invalid([&] { auto x=decoded; x.receiver_id={"receiver:OFF"}; r::validate_rf_completion(work,{x}); });
        invalid([&] { auto x=decoded; x.decoded_payload_reference="other"; r::validate_rf_completion(work,{x}); });
        invalid([&] { auto x=carrier; x.reception_id=r::ReceptionId{"not-decoded"}; r::validate_rf_completion(work,{x}); });
        invalid([&] { auto x=decoded; x.disposition=r::DecodeDisposition::Failed; r::validate_rf_completion(work,{x}); });
        invalid([&] { r::validate_rf_completion(work,{decoded,decoded}); });
        invalid([&] { auto w=work; w.observation_capacity=1; r::validate_rf_completion(w,{carrier,decoded}); });
        invalid([&] { auto w=work; w.schema_version=2; r::validate_rf_submission(w); });
        std::cout << "PASS host RF interface only: stable typed identities, causal availability, resident-state deltas, bounded completions; NO RF computation\n";
        return 0;
    } catch(const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
