#pragma once

#include "event.h"

namespace aismixer::lab::runtime {

struct Fixture {
    std::string provenance;
    UtcAnchor utc_anchor;
    std::vector<ReceiverId> receivers;
    std::vector<Event> events;
};
// SYNTHETIC INFRASTRUCTURE TEST, NOT AIS RADIO SIMULATION. Its receiver
// outcomes are explicit scripted truth; the stored B3 golden is never encoded.
Fixture make_synthetic_fixture();

}  // namespace aismixer::lab::runtime
