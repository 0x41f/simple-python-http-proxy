#!/usr/bin/python3

import logging
import queue
import select
import socket
import sys

logger = logging.basicConfig(level=logging.DEBUG)

PROXY_RESPONSE = b'HTTP/1.1 200 Connection Established\r\nProxy-agent: AuthProxy\r\n\r\n'


def unpack_request(message):
    """
    :param message: an http request string
    :return: tuple containing http method, address and port
    """
    space_separated = message.decode('utf-8').split(' ')
    method = space_separated[0]
    if 'http' in space_separated[1]:
        dest_addr, port = space_separated[1].split('/')[2], '80'
    else:
        dest_addr, port = space_separated[1].split(':')
    return method, dest_addr, port


class Server:
    """
    Listens for incoming connections, accepts and return connected sockets.
    """

    def __init__(self, listen_port=8080):
        self.listen_port = listen_port
        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.s.setblocking(False)

    def start(self):
        try:
            self.s.bind(('127.0.0.1', self.listen_port))
        except socket.error as err:
            logging.debug(f'Bind Error: {err}')
            return False
        self.s.listen(5)
        logging.debug(f'Listening on 127.0.0.1:{self.listen_port}')
        return True

    def stop(self):
        self.s.shutdown(socket.SHUT_RDWR)
        self.s.close()

    def get_conn(self):
        return self.s.accept()


class SocketHandler:
    """
    Base class encapsulating a single socket, an outgoing queue,
    and handler methods for send and rcv.
    """

    def __init__(self, sock):
        self.s = sock
        self.outq = queue.Queue()

    def start(self):
        # Subclass must re-write this method
        pass

    def stop(self):
        self.s.shutdown(socket.SHUT_RDWR)
        self.s.close()

    def send(self, msg):
        self.s.sendall(msg)

    def receive(self):
        try:
            received = self.s.recv(4096)
        except socket.error as e:
            logging.debug(f'Connection closed: {e}')
            return None
        return received


class Inside(SocketHandler):
    """
    Client-facing socket handler
    """

    def __init__(self, sock):
        super().__init__(sock)
        self.s.setblocking(False)
        self.established = False


class Outside(SocketHandler):
    """
    Remote facing socket handler
    """

    def __init__(self, sock):
        super().__init__(sock)
        logging.debug(f'Connecting socket {self.s}')

    def start(self, remote_addr, remote_port):
        try:
            self.s.connect((remote_addr, remote_port))
        except (socket.error, socket.timeout) as err:
            logging.debug(f'Connect error: {err}')
            return False
        self.s.setblocking(False)
        return True


def main():
    parent_object = {}
    partner_socket = {}

    server = Server()
    if not server.start():
        sys.exit('Uh-oh, something went wrong...')

    # map socket to its parent for later reference
    parent_object[server.s] = server

    # set up initial polling lists
    inputs, outputs = [server.s], []

    while True:
        try:
            readable, writable, exceptional = select.select(inputs, outputs, inputs)

            for r in readable:
                # If server socket readable, accept incoming connection & create object
                if isinstance(parent_object[r], Server):
                    conn, address = parent_object[r].get_conn()
                    logging.debug(f'Incoming connection from {address}')
                    inside = Inside(conn)

                    # map socket to parent object
                    parent_object[inside.s] = inside

                    # add incoming connection to input list
                    inputs.append(inside.s)

                elif isinstance(parent_object[r], Inside):
                    data = parent_object[r].receive()

                    if data:
                        # establish new proxy tunnel
                        if not parent_object[r].established:
                            (method, dest_addr, dest_port) = unpack_request(data)
                            logging.debug(f'Received {method} request to {dest_addr}:{dest_port}')
                            outside = Outside(socket.socket(socket.AF_INET, socket.SOL_SOCKET))
                            logging.debug(f'Outside socket: {outside.s.fileno()}')
                            # if outside connection fails, tear down inside connection
                            logging.debug('Connecting to remote server...')
                            if not outside.start(dest_addr, int(dest_port)):
                                inputs.remove(r)
                                parent_object[r].stop()
                                logging.debug('Connection failed. Tearing down...')
                                del outside.s
                                del outside
                                continue
                            else:
                                logging.debug(f'Connected to {dest_addr}:{dest_port}')
                                # map to parent object
                                parent_object[outside.s] = outside

                                # map the inside and outside sockets to each other
                                partner_socket[r], partner_socket[outside.s] = outside.s, r

                                # if client aware of connecting to proxy, give default 200
                                if method == 'CONNECT':
                                    parent_object[r].send(PROXY_RESPONSE)

                                # otherwise, forward transparently
                                else:
                                    if partner_socket[r] not in outputs:
                                        outputs.append(partner_socket[r])
                                    parent_object[partner_socket[r]].outq.put(data)

                            parent_object[r].established = True

                        # also just forward if proxy already established
                        else:
                            if partner_socket[r] not in outputs:
                                outputs.append(partner_socket[r])
                            parent_object[partner_socket[r]].outq.put(data)
                    else:
                        pass
                # if outside, just gotta receive and put into corresponding inside's queue
                else:
                    data = parent_object[r].receive()
                    if data:
                        if partner_socket[r] not in outputs:
                            outputs.append(partner_socket[r])
                        parent_object[partner_socket[r]].outq.put(data)
                    else:
                        pass
            for w in writable:
                while True:
                    try:
                        data = parent_object[w].outq.get_nowait()
                        parent_object[w].send(data)
                    except queue.Empty:
                        outputs.remove(w)
                        break

                if w not in inputs:
                    inputs.append(w)

            for e in exceptional:
                logging.debug(f'Hit exception on {e}')
                inputs.remove(e)

        except KeyboardInterrupt:
            server.stop()
            for i in partner_socket.keys():
                try:
                    parent_object[i].stop()
                except:
                    pass


if __name__ == '__main__':
    main()
