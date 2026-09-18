// Shared by the privileged installer and runtime. Accept only canonical IPv4
// CIDRs and require the routed subnet to be the interface's exact network.
func networkAddressPair(_ address: String, _ subnet: String) -> Bool {
    func parse(_ value: String) -> (UInt32, Int)? {
        let parts = value.split(separator: "/", omittingEmptySubsequences: false)
        guard parts.count == 2, let prefix = Int(parts[1]), (1...32).contains(prefix),
              String(prefix) == parts[1] else { return nil }
        let octets = parts[0].split(separator: ".", omittingEmptySubsequences: false)
        guard octets.count == 4 else { return nil }
        var number: UInt32 = 0
        for octet in octets {
            guard let byte = UInt8(octet), String(byte) == octet else { return nil }
            number = (number << 8) | UInt32(byte)
        }
        return (number, prefix)
    }
    guard let (host, prefix) = parse(address), let (network, networkPrefix) = parse(subnet),
          prefix == networkPrefix else { return false }
    let mask = UInt32.max << (32 - prefix)
    return network & mask == network && host & mask == network
}
