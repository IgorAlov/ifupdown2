#!/usr/bin/env python

from ipaddr import IPv4Address, IPv6Address
from nlpacket import *
from select import select
from struct import pack, unpack
from tabulate import tabulate
import logging
import os
import socket

log = logging.getLogger(__name__)


class NetlinkError(Exception):
    pass


class NetlinkNoAddressError(Exception):
    pass


class InvalidInterfaceNameVlanCombo(Exception):
    pass


class Sequence(object):

    def __init__(self):
        self._next = 0

    def next(self):
        self._next += 1
        return self._next


class NetlinkManager(object):

    def __init__(self):
        self.pid = os.getpid()
        self.sequence = Sequence()
        self.shutdown_flag = False
        self.ifindexmap = {}
        self.tx_socket = None

        # debugs
        self.debug = {}
        self.debug_link(False)
        self.debug_address(False)
        self.debug_neighbor(False)
        self.debug_route(False)

    def __str__(self):
        return 'NetlinkManager'

    def signal_term_handler(self, signal, frame):
        log.info("NetlinkManager: Caught SIGTERM")
        self.shutdown_flag = True

    def signal_int_handler(self, signal, frame):
        log.info("NetlinkManager: Caught SIGINT")
        self.shutdown_flag = True

    def shutdown(self):
        if self.tx_socket:
            self.tx_socket.close()
            self.tx_socket = None
        log.info("NetlinkManager: shutdown complete")

    def _debug_set_clear(self, msg_types, enabled):
        """
        Enable or disable debugs for all msgs_types messages
        """

        for x in msg_types:
            if enabled:
                self.debug[x] = True
            else:
                if x in self.debug:
                    del self.debug[x]

    def debug_link(self, enabled):
        self._debug_set_clear((RTM_NEWLINK, RTM_DELLINK, RTM_GETLINK, RTM_SETLINK), enabled)

    def debug_address(self, enabled):
        self._debug_set_clear((RTM_NEWADDR, RTM_DELADDR, RTM_GETADDR), enabled)

    def debug_neighbor(self, enabled):
        self._debug_set_clear((RTM_NEWNEIGH, RTM_DELNEIGH, RTM_GETNEIGH), enabled)

    def debug_route(self, enabled):
        self._debug_set_clear((RTM_NEWROUTE, RTM_DELROUTE, RTM_GETROUTE), enabled)

    def debug_this_packet(self, mtype):
        if mtype in self.debug:
            return True
        return False

    def tx_socket_allocate(self):
        """
        The TX socket is used for install requests, sending RTM_GETXXXX
        requests, etc
        """
        self.tx_socket = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)
        self.tx_socket.bind((self.pid, 0))

    def tx_nlpacket_raw(self, message):
        """
        TX a bunch of concatenated nlpacket.messages....do NOT wait for an ACK
        """
        if not self.tx_socket:
            self.tx_socket_allocate()
        self.tx_socket.sendall(message)

    def tx_nlpacket(self, nlpacket):
        """
        TX a netlink packet but do NOT wait for an ACK
        """
        if not nlpacket.message:
            log.error('You must first call build_message() to create the packet')
            return

        if not self.tx_socket:
            self.tx_socket_allocate()
        self.tx_socket.sendall(nlpacket.message)

    def tx_nlpacket_get_response(self, nlpacket):

        if not nlpacket.message:
            log.error('You must first call build_message() to create the packet')
            return

        if not self.tx_socket:
            self.tx_socket_allocate()
        self.tx_socket.sendall(nlpacket.message)

        # If debugs are enabled we will print the contents of the
        # packet via the decode_packet call...so avoid printing
        # two messages for one packet.
        if not nlpacket.debug:
            log.info("TXed %12s, pid %d, seq %d, %d bytes" %
                     (nlpacket.get_type_string(), nlpacket.pid, nlpacket.seq, nlpacket.length))

        header_PACK = NetlinkPacket.header_PACK
        header_LEN = NetlinkPacket.header_LEN
        null_read = 0
        MAX_NULL_READS = 30
        msgs = []

        # Now listen to our socket and wait for the reply
        while True:

            if self.shutdown_flag:
                log.info('shutdown flag is True, exiting')
                return msgs

            # Only block for 1 second so we can wake up to see if self.shutdown_flag is True
            (readable, writeable, exceptional) = select([self.tx_socket, ], [], [self.tx_socket, ], 1)

            if not readable:
                null_read += 1

                # Safety net to make sure we do not spend too much time in
                # this while True loop
                if null_read >= MAX_NULL_READS:
                    log.warning('Socket was not readable for %d attempts' % null_read)
                    return msgs

                continue

            for s in readable:
                data = s.recv(4096)

                if not data:
                    log.info('RXed zero length data, the socket is closed')
                    return msgs

                while data:

                    # Extract the length, etc from the header
                    (length, msgtype, flags, seq, pid) = unpack(header_PACK, data[:header_LEN])

                    debug_str = "RXed %12s, pid %d, seq %d, %d bytes" % (NetlinkPacket.type_to_string[msgtype], pid, seq, length)

                    # This shouldn't happen but it would be nice to be aware of it if it does
                    if pid != nlpacket.pid:
                        log.debug(debug_str + '...we are not interested in this pid %s since ours is %s' %
                                    (pid, nlpacket.pid))
                        data = data[length:]
                        continue
                    if seq != nlpacket.seq:
                        log.debug(debug_str + '...we are not interested in this seq %s since ours is %s' %
                                    (seq, nlpacket.seq))
                        data = data[length:]
                        continue
                    # See if we RXed an ACK for our RTM_GETXXXX
                    if msgtype == NLMSG_DONE:
                        log.debug(debug_str + '...this is an ACK')
                        return msgs

                    elif msgtype == NLMSG_ERROR:

                        # The error code is a signed negative number.
                        error_code = abs(unpack('=i', data[header_LEN:header_LEN+4])[0])
                        msg = Error(msgtype, nlpacket.debug)
                        msg.decode_packet(length, flags, seq, pid, data)

                        debug_str += ", error code %s" % msg.error_to_string.get(error_code)

                        # 0 is NLE_SUCCESS...everything else is a true error
                        if error_code:
                            if error_code == Error.NLE_NOADDR:
                                raise NetlinkNoAddressError(debug_str)
                            else:
                                raise NetlinkError(debug_str)
                        else:
                            log.info(debug_str + '...this is an ACK')
                            return msgs

                    # No ACK...create a nlpacket object and append it to msgs
                    else:

                        # If debugs are enabled we will print the contents of the
                        # packet via the decode_packet call...so avoid printing
                        # two messages for one packet.
                        if not nlpacket.debug:
                            log.info(debug_str)

                        if msgtype == RTM_NEWLINK or msgtype == RTM_DELLINK:
                            msg = Link(msgtype, nlpacket.debug)

                        elif msgtype == RTM_NEWADDR or msgtype == RTM_DELADDR:
                            msg = Address(msgtype, nlpacket.debug)

                        elif msgtype == RTM_NEWNEIGH or msgtype == RTM_DELNEIGH:
                            msg = Neighbor(msgtype, nlpacket.debug)

                        elif msgtype == RTM_NEWROUTE or msgtype == RTM_DELROUTE:
                            msg = Route(msgtype, nlpacket.debug)

                        else:
                            raise Exception("RXed unknown netlink message type %s" % msgtype)

                        msg.decode_packet(length, flags, seq, pid, data)
                        msgs.append(msg)

                    data = data[length:]

    def ip_to_afi(self, ip):
        type_ip = type(ip)

        if type_ip == IPv4Address:
            return socket.AF_INET
        elif type_ip == IPv6Address:
            return socket.AF_INET6
        else:
            raise Exception("%s is an invalid IP type" % type_ip)

    def request_dump(self, rtm_type, family, debug):
        """
        Issue a RTM_GETROUTE, etc with the NLM_F_DUMP flag
        set and return the results
        """

        if rtm_type == RTM_GETADDR:
            msg = Address(rtm_type, debug)
            msg.body = pack('Bxxxi', family, 0)

        elif rtm_type == RTM_GETLINK:
            msg = Link(rtm_type, debug)
            msg.body = pack('Bxxxiii', family, 0, 0, 0)

        elif rtm_type == RTM_GETNEIGH:
            msg = Neighbor(rtm_type, debug)
            msg.body = pack('Bxxxii', family, 0, 0)

        elif rtm_type == RTM_GETROUTE:
            msg = Route(rtm_type, debug)
            msg.body = pack('Bxxxii', family, 0, 0)

        else:
            log.error("request_dump RTM_GET %s is not supported" % rtm_type)
            return None

        msg.flags = NLM_F_REQUEST | NLM_F_DUMP
        msg.attributes = {}
        msg.build_message(self.sequence.next(), self.pid)
        return self.tx_nlpacket_get_response(msg)

    # ======
    # Routes
    # ======
    def _routes_add_or_delete(self, add_route, routes, ecmp_routes, table, protocol, route_scope, route_type):

        def tx_or_concat_message(total_message, route):
            """
            Adding an ipv4 route only takes 60 bytes, if we are adding thousands
            of them this can add up to a lot of send calls.  Concat several of
            them together before TXing.
            """

            if not total_message:
                total_message = route.message
            else:
                total_message += route.message

            if len(total_message) >= PACKET_CONCAT_SIZE:
                self.tx_nlpacket_raw(total_message)
                total_message = None

            return total_message

        if add_route:
            rtm_command = RTM_NEWROUTE
        else:
            rtm_command = RTM_DELROUTE

        total_message = None
        PACKET_CONCAT_SIZE = 16384
        debug = rtm_command in self.debug

        if routes:
            for (afi, ip, mask, nexthop, interface_index) in routes:
                route = Route(rtm_command, debug)
                route.flags = NLM_F_REQUEST | NLM_F_CREATE
                route.body = pack('BBBBBBBBi', afi, mask, 0, 0, table, protocol,
                                  route_scope, route_type, 0)
                route.family = afi
                route.add_attribute(Route.RTA_DST, ip)
                if nexthop:
                    route.add_attribute(Route.RTA_GATEWAY, nexthop)
                route.add_attribute(Route.RTA_OIF, interface_index)
                route.build_message(self.sequence.next(), self.pid)
                total_message = tx_or_concat_message(total_message, route)

            if total_message:
                self.tx_nlpacket_raw(total_message)

        if ecmp_routes:

            for (route_key, value) in ecmp_routes.iteritems():
                (afi, ip, mask) = route_key

                route = Route(rtm_command, debug)
                route.flags = NLM_F_REQUEST | NLM_F_CREATE
                route.body = pack('BBBBBBBBi', afi, mask, 0, 0, table, protocol,
                                  route_scope, route_type, 0)
                route.family = afi
                route.add_attribute(Route.RTA_DST, ip)
                route.add_attribute(Route.RTA_MULTIPATH, value)
                route.build_message(self.sequence.next(), self.pid)
                total_message = tx_or_concat_message(total_message, route)

            if total_message:
                self.tx_nlpacket_raw(total_message)

    def routes_add(self, routes, ecmp_routes,
                   table=Route.RT_TABLE_MAIN,
                   protocol=Route.RT_PROT_XORP,
                   route_scope=Route.RT_SCOPE_UNIVERSE,
                   route_type=Route.RTN_UNICAST):
        self._routes_add_or_delete(True, routes, ecmp_routes, table, protocol, route_scope, route_type)

    def routes_del(self, routes, ecmp_routes,
                   table=Route.RT_TABLE_MAIN,
                   protocol=Route.RT_PROT_XORP,
                   route_scope=Route.RT_SCOPE_UNIVERSE,
                   route_type=Route.RTN_UNICAST):
        self._routes_add_or_delete(False, routes, ecmp_routes, table, protocol, route_scope, route_type)

    def route_get(self, ip, debug=False):
        """
        ip must be one of the following:
        - IPv4Address
        - IPv6Address
        """
        # Transmit a RTM_GETROUTE to query for the route we want
        route = Route(RTM_GETROUTE, debug)
        route.flags = NLM_F_REQUEST | NLM_F_ACK

        # Set everything in the service header as 0 other than the afi
        afi = self.ip_to_afi(ip)
        route.body = pack('Bxxxxxxxi', afi, 0)
        route.family = afi
        route.add_attribute(Route.RTA_DST, ip)
        route.build_message(self.sequence.next(), self.pid)
        return self.tx_nlpacket_get_response(route)

    def routes_dump(self, family=socket.AF_UNSPEC, debug=True):
        return self.request_dump(RTM_GETROUTE, family, debug)

    def routes_print(self, routes):
        """
        Use tabulate to print a table of 'routes'
        """
        header = ['Prefix', 'nexthop', 'ifindex']
        table = []

        for x in routes:
            if Route.RTA_DST not in x.attributes:
                log.warning("Route is missing RTA_DST")
                continue

            table.append(('%s/%d' % (x.attributes[Route.RTA_DST].value, x.src_len),
                          str(x.attributes[Route.RTA_GATEWAY].value) if Route.RTA_GATEWAY in x.attributes else None,
                          x.attributes[Route.RTA_OIF].value))

        print tabulate(table, header, tablefmt='simple') + '\n'

    # =====
    # Links
    # =====
    def _get_iface_by_name(self, ifname):
        """
        Return a Link object for ifname
        """
        debug = RTM_GETLINK in self.debug

        link = Link(RTM_GETLINK, debug)
        link.flags = NLM_F_REQUEST | NLM_F_ACK
        link.body = pack('=Bxxxiii', socket.AF_UNSPEC, 0, 0, 0)
        link.add_attribute(Link.IFLA_IFNAME, ifname)
        link.build_message(self.sequence.next(), self.pid)

        try:
            return self.tx_nlpacket_get_response(link)[0]

        except NetlinkNoAddressError:
            log.info("Netlink did not find interface %s" % ifname)
            return None

    def get_iface_index(self, ifname):
        """
        Return the interface index for ifname
        """
        iface = self._get_iface_by_name(ifname)

        if iface:
            return iface.ifindex
        return None

    def _link_add(self, ifindex, ifname, kind, ifla_info_data):
        """
        Build and TX a RTM_NEWLINK message to add the desired interface
        """
        debug = RTM_NEWLINK in self.debug

        link = Link(RTM_NEWLINK, debug)
        link.flags = NLM_F_CREATE | NLM_F_REQUEST
        link.body = pack('Bxxxiii', socket.AF_UNSPEC, 0, 0, 0)
        link.add_attribute(Link.IFLA_IFNAME, ifname)
        link.add_attribute(Link.IFLA_LINK, ifindex)
        link.add_attribute(Link.IFLA_LINKINFO, {
            Link.IFLA_INFO_KIND: kind,
            Link.IFLA_INFO_DATA: ifla_info_data
        })
        link.build_message(self.sequence.next(), self.pid)
        return self.tx_nlpacket(link)

    def link_add_vlan(self, ifindex, ifname, vlanid):
        """
        ifindex is the index of the parent interface that this sub-interface
        is being added to
        """

        '''
        If you name an interface swp2.17 but assign it to vlan 12, the kernel
        will return a very misleading NLE_MSG_OVERFLOW error.  It only does
        this check if the ifname uses dot notation.

        Do this check here so we can provide a more intuitive error
        '''
        if '.' in ifname:
            ifname_vlanid = int(ifname.split('.')[-1])

            if ifname_vlanid != vlanid:
                raise InvalidInterfaceNameVlanCombo("Interface %s must belong "
                                                    "to VLAN %d (VLAN %d was requested)" %
                                                    (ifname, ifname_vlanid, vlanid))

        return self._link_add(ifindex, ifname, 'vlan', {Link.IFLA_VLAN_ID: vlanid})

    def link_add_macvlan(self, ifindex, ifname):
        """
        ifindex is the index of the parent interface that this sub-interface
        is being added to
        """
        return self._link_add(ifindex, ifname, 'macvlan', {Link.IFLA_MACVLAN_MODE: Link.MACVLAN_MODE_PRIVATE})

    def _link_bridge_vlan(self, msgtype, ifindex, vlanid, pvid, untagged, master):
        """
        Build and TX a RTM_NEWLINK message to add the desired interface
        """

        if master:
            flags = 0
        else:
            flags = Link.BRIDGE_FLAGS_SELF

        if pvid:
            vflags = Link.BRIDGE_VLAN_INFO_PVID | Link.BRIDGE_VLAN_INFO_UNTAGGED
        elif untagged:
            vflags = Link.BRIDGE_VLAN_INFO_UNTAGGED
        else:
            vflags = 0

        debug = msgtype in self.debug

        link = Link(msgtype, debug)
        link.flags = NLM_F_REQUEST | NLM_F_ACK
        link.body = pack('Bxxxiii', socket.AF_BRIDGE, ifindex, 0, 0)
        link.add_attribute(Link.IFLA_AF_SPEC, {
            Link.IFLA_BRIDGE_FLAGS: flags,
            Link.IFLA_BRIDGE_VLAN_INFO: (vflags, vlanid)
        })
        link.build_message(self.sequence.next(), self.pid)
        return self.tx_nlpacket(link)

    def link_add_bridge_vlan(self, ifindex, vlanid, pvid=False, untagged=False, master=False):
        self._link_bridge_vlan(RTM_SETLINK, ifindex, vlanid, pvid, untagged, master)

    def link_del_bridge_vlan(self, ifindex, vlanid, pvid=False, untagged=False, master=False):
        self._link_bridge_vlan(RTM_DELLINK, ifindex, vlanid, pvid, untagged, master)

    def link_set_updown(self, ifname, state):
        """
        Either bring ifname up or take it down
        """

        if state == 'up':
            if_flags = Link.IFF_UP
        elif state == 'down':
            if_flags = 0
        else:
            raise Exception('Unsupported state %s, valid options are "up" and "down"' % state)

        debug = RTM_NEWLINK in self.debug
        if_change = Link.IFF_UP

        link = Link(RTM_NEWLINK, debug)
        link.flags = NLM_F_REQUEST
        link.body = pack('=BxxxiLL', socket.AF_UNSPEC, 0, if_flags, if_change)
        link.add_attribute(Link.IFLA_IFNAME, ifname)
        link.build_message(self.sequence.next(), self.pid)
        return self.tx_nlpacket(link)

    def link_set_protodown(self, ifname, state):
        """
        Either bring ifname up or take it down by setting IFLA_PROTO_DOWN on or off
        """
        flags = 0
        protodown = 1 if state == "on" else 0

        debug = RTM_NEWLINK in self.debug

        link = Link(RTM_NEWLINK, debug)
        link.flags = NLM_F_REQUEST
        link.body = pack('=BxxxiLL', socket.AF_UNSPEC, 0, 0, 0)
        link.add_attribute(Link.IFLA_IFNAME, ifname)
        link.add_attribute(Link.IFLA_PROTO_DOWN, protodown)
        link.build_message(self.sequence.next(), self.pid)
        return self.tx_nlpacket(link)

    # =========
    # Neighbors
    # =========
    def neighbor_add(self, afi, ifindex, ip, mac):
        debug = RTM_NEWNEIGH in self.debug
        service_hdr_flags = 0

        nbr = Neighbor(RTM_NEWNEIGH, debug)
        nbr.flags = NLM_F_CREATE | NLM_F_REQUEST
        nbr.family = afi
        nbr.body = pack('=BxxxiHBB', afi, ifindex, Neighbor.NUD_REACHABLE, service_hdr_flags, Route.RTN_UNICAST)
        nbr.add_attribute(Neighbor.NDA_DST, ip)
        nbr.add_attribute(Neighbor.NDA_LLADDR, mac)
        nbr.build_message(self.sequence.next(), self.pid)
        return self.tx_nlpacket(nbr)

    def neighbor_del(self, afi, ifindex, ip, mac):
        debug = RTM_DELNEIGH in self.debug
        service_hdr_flags = 0

        nbr = Neighbor(RTM_DELNEIGH, debug)
        nbr.flags = NLM_F_REQUEST
        nbr.family = afi
        nbr.body = pack('=BxxxiHBB', afi, ifindex, Neighbor.NUD_REACHABLE, service_hdr_flags, Route.RTN_UNICAST)
        nbr.add_attribute(Neighbor.NDA_DST, ip)
        nbr.add_attribute(Neighbor.NDA_LLADDR, mac)
        nbr.build_message(self.sequence.next(), self.pid)
        return self.tx_nlpacket(nbr)