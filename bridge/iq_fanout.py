"""Proxy fan-out IQ pour decodeurs PARALLELES sur la machine compagne (offload,
2026-11-01). rtl_tcp ne sert QU'UN client (verifie : un 2e dsd-neo reste en
"input stalled"). Pour faire tourner plusieurs dsd-neo EN MEME TEMPS sur le meme
flux (modele arbitre local : chaque mode epingle confirme ou non, on garde le
gagnant, zero "float" de l'auto-detection), ce proxy ouvre UNE connexion vers le
rtl_tcp du Pi et rediffuse l'IQ a N clients dsd-neo locaux.

Protocole rtl_tcp : le serveur envoie 12 octets d'entete (magic "RTL0" + type de
tuner + nb de gains, en big-endian), puis l'IQ brut (paires d'octets I/Q uint8).
Le client envoie des commandes de 5 octets (1 octet commande + 4 octets BE :
frequence, taux, gain...). Ici : on relaie l'IQ a tous les clients ; les commandes
du PREMIER client connecte (le "commandant") sont transmises en amont (il regle
frequence/taux/gain pour la fenetre), celles des autres sont ignorees -- tous nos
clients demandent les MEMES reglages (meme fenetre), donc c'est deterministe."""
import socket
import struct
import threading


def _recvn(sock, n):
    buf = b""
    while len(buf) < n:
        bloc = sock.recv(n - len(buf))
        if not bloc:
            break
        buf += bloc
    return buf


class FanOut:
    def __init__(self, up_host, up_port):
        self.up_host = up_host
        self.up_port = int(up_port)
        self.up = None
        self.header = b""
        self.srv = None
        self.port = None
        self._clients = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._commandant = None

    def start(self, timeout=10):
        """Connecte l'amont (rtl_tcp du Pi), lit l'entete, ouvre un port local.
        Leve si l'amont est injoignable."""
        self.up = socket.create_connection((self.up_host, self.up_port), timeout=timeout)
        self.up.settimeout(None)
        self.header = _recvn(self.up, 12)
        if len(self.header) != 12:
            raise OSError("entete rtl_tcp amont incomplete")
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.srv.settimeout(0.5)
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._pump_iq, daemon=True).start()
        return self.port

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self._lock:
                commandant = self._commandant is None
                if commandant:
                    self._commandant = conn
                self._clients.append(conn)
            try:
                conn.sendall(self.header)
            except OSError:
                self._drop(conn)
                continue
            threading.Thread(target=self._client_cmds, args=(conn, commandant), daemon=True).start()

    def _client_cmds(self, conn, commandant):
        # Commandes 5 octets. Celles du commandant reglent l'amont ; les autres
        # sont lues puis ignorees (reglages identiques).
        while not self._stop.is_set():
            cmd = _recvn(conn, 5)
            if len(cmd) != 5:
                break
            if commandant and self.up is not None:
                try:
                    self.up.sendall(cmd)
                except OSError:
                    break
        self._drop(conn)

    def _pump_iq(self):
        try:
            while not self._stop.is_set():
                data = self.up.recv(262144)
                if not data:
                    break
                with self._lock:
                    clients = list(self._clients)
                for c in clients:
                    try:
                        c.sendall(data)
                    except OSError:
                        self._drop(c)
        except OSError:
            pass
        finally:
            self.stop()

    def _drop(self, conn):
        with self._lock:
            if conn in self._clients:
                self._clients.remove(conn)
            if self._commandant is conn:
                self._commandant = None
        try:
            conn.close()
        except OSError:
            pass

    def stop(self):
        if self._stop.is_set():
            return
        self._stop.set()
        with self._lock:
            socks = list(self._clients)
            self._clients = []
        for s in socks:
            try:
                s.close()
            except OSError:
                pass
        for s in (self.srv, self.up):
            try:
                if s:
                    s.close()
            except OSError:
                pass
