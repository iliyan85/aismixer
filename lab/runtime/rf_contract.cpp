#include "rf_contract.h"

#include <algorithm>
#include <map>
#include <set>
#include <stdexcept>

namespace aismixer::lab::runtime {
void validate_rf_submission(const RfWorkSubmission& work) {
    if (work.schema_version != 1 || work.transmissions.size() > kMaxRfTransmissions ||
        work.receiver_changes.size() > kMaxRfReceivers || work.active_receivers.size() > kMaxRfReceivers ||
        work.observation_capacity == 0 ||
        work.observation_capacity > kMaxRfObservations) {
        throw std::invalid_argument("RF work schema/capacity is unsupported");
    }
    std::set<std::string> transmissions, receivers, active;
    std::map<std::string, std::vector<std::pair<SimulationInstant, SimulationInstant>>> station_intervals;
    for (const auto& receiver : work.active_receivers) {
        validate_identity(receiver.value, "RF active receiver");
        if (!active.insert(receiver.value).second) throw std::invalid_argument("RF duplicate active receiver");
    }
    for (const auto& tx : work.transmissions) {
        validate_identity(tx.transmission_id.value, "RF transmission_id");
        validate_identity(tx.station_id, "RF station_id");
        validate_identity(tx.payload_reference, "RF payload_reference");
        if (!transmissions.insert(tx.transmission_id.value).second || tx.channel > 1 ||
            tx.start < work.commit_watermark || tx.end <= tx.start) {
            throw std::invalid_argument("RF duplicate TX, channel, interval or committed-prefix violation");
        }
        station_intervals[tx.station_id].emplace_back(tx.start, tx.end);
    }
    for (auto& station : station_intervals) {
        auto& intervals = station.second;
        std::sort(intervals.begin(), intervals.end(), [](const auto& a, const auto& b) {
            return a.first != b.first ? a.first < b.first : a.second < b.second;
        });
        for (std::size_t i = 1; i < intervals.size(); ++i)
            if (intervals[i].first < intervals[i - 1].second)
                throw std::invalid_argument("RF station has overlapping physical TX intervals");
    }
    for (const auto& rx : work.receiver_changes) {
        validate_identity(rx.receiver_id.value, "RF receiver_id");
        if (!receivers.insert(rx.receiver_id.value).second ||
            rx.enabled != (active.count(rx.receiver_id.value) != 0) || rx.height_datum != "WGS84_ellipsoid" ||
            rx.longitude_ndeg < -180000000000LL || rx.longitude_ndeg > 180000000000LL ||
            rx.latitude_ndeg < -90000000000LL || rx.latitude_ndeg > 90000000000LL ||
            rx.antenna_height_mm < -1000000 || rx.antenna_height_mm > 10000000) {
            throw std::invalid_argument("RF receiver identity/location/height datum is invalid");
        }
    }
}

void validate_rf_completion(const RfWorkSubmission& work,
                            const std::vector<CompletedRfObservation>& observations) {
    validate_rf_submission(work);
    if (observations.size() > work.observation_capacity) {
        throw std::invalid_argument("RF compact completion exceeds submitted capacity");
    }
    std::map<std::string, const RfTransmissionWork*> transmissions;
    std::set<std::string> receivers, observation_ids, reception_ids;
    std::set<std::pair<std::string, std::string>> receiver_results;
    for (const auto& tx : work.transmissions) transmissions.emplace(tx.transmission_id.value, &tx);
    for (const auto& receiver : work.active_receivers) receivers.insert(receiver.value);
    for (const auto& observation : observations) {
        validate_identity(observation.observation_id.source_id, "RF observation source");
        if (observation.observation_id.sequence == 0) throw std::invalid_argument("RF observation sequence must be positive");
        const auto key = observation.observation_id.source_id + "/" + std::to_string(observation.observation_id.sequence);
        const auto tx = transmissions.find(observation.transmission_id.value);
        if (!observation_ids.insert(key).second || tx == transmissions.end() ||
            !receivers.count(observation.receiver_id.value) ||
            observation.availability_time < tx->second->start ||
            observation.availability_time < work.commit_watermark) {
            throw std::invalid_argument("RF completion identity/availability has no eligible submitted work");
        }
        if (observation.observing_station_id) validate_identity(*observation.observing_station_id, "RF local observing station");
        if (observation.disposition != DecodeDisposition::Undecoded &&
            observation.disposition != DecodeDisposition::Decoded &&
            observation.disposition != DecodeDisposition::Failed) {
            throw std::invalid_argument("RF decode disposition is unsupported");
        }
        if (observation.disposition == DecodeDisposition::Undecoded) {
            if (observation.reception_id || observation.decoded_payload_reference)
                throw std::invalid_argument("Undecoded RF observation cannot imply reception or decoded payload");
        } else {
            if (!observation.reception_id || observation.availability_time < tx->second->end)
                throw std::invalid_argument("RF decode result precedes complete TX or lacks reception identity");
            validate_identity(observation.reception_id->value, "RF reception_id");
            if (!reception_ids.insert(observation.reception_id->value).second)
                throw std::invalid_argument("RF duplicate reception identity");
            if (!receiver_results.emplace(observation.transmission_id.value, observation.receiver_id.value).second)
                throw std::invalid_argument("RF duplicate decode result for one TX/receiver pair");
            if (observation.disposition == DecodeDisposition::Decoded) {
                if (!observation.decoded_payload_reference ||
                    *observation.decoded_payload_reference != tx->second->payload_reference)
                    throw std::invalid_argument("RF decoded result must reference the submitted immutable payload");
            } else if (observation.decoded_payload_reference) {
                throw std::invalid_argument("Failed RF decoding cannot supply a decoded payload");
            }
        }
    }
}
}  // namespace aismixer::lab::runtime
