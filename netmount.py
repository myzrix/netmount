#!/usr/bin/env python3
"""netmount — monte automatiquement des partages réseau (SMB/NFS) quand
leur serveur est joignable, et les démonte quand il ne l'est plus.

Pensé pour un portable qui change de réseau : un partage dont le serveur
a disparu est démonté de force (umount -f -l) avant qu'il ne fige les
applications.

  python3 netmount.py          ouvre la fenêtre
  python3 netmount.py --tray   démarre caché dans la zone de notification

Les opérations root passent par /usr/local/sbin/netmount-helper (voir
install.sh), appelé via `sudo -n`.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import warnings

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)

APP_ID = "fr.myz.netmount"
HERE = os.path.dirname(os.path.realpath(__file__))
HOME = os.path.expanduser("~")
CONF_DIR = os.path.join(GLib.get_user_config_dir(), "netmount")
CONF_FILE = os.path.join(CONF_DIR, "config.json")
CRED_DIR = os.path.join(CONF_DIR, "creds")
HELPER = "/usr/local/sbin/netmount-helper"
AUTOSTART = os.path.join(GLib.get_user_config_dir(), "autostart", "netmount.desktop")
LAUNCHER = os.path.join(GLib.get_user_data_dir(), "applications", "netmount.desktop")

INTERVAL = 10        # secondes entre deux vérifications
RETRY_AFTER = 30     # délai avant de retenter un montage échoué
PORTS = {"cifs": 445, "nfs": 2049}
TYPE_LABELS = [("cifs", "SMB / Windows (Samba)"), ("nfs", "NFS")]


# --------------------------------------------------------------- système

def resolve(path):
    """Chemin absolu sans stat sur la feuille (un montage mort bloquerait)."""
    path = os.path.expanduser(path).rstrip("/")
    return os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))


def mounted_points():
    pts = set()
    with open("/proc/self/mounts") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                mp = parts[1]
                for oct_ in ("\\040", "\\011", "\\012", "\\134"):
                    mp = mp.replace(oct_, chr(int(oct_[1:], 8)))
                pts.add(mp)
    return pts


def is_mounted(path):
    return resolve(path) in mounted_points()


def reachable(host, port, tries=2):
    for _ in range(tries):
        try:
            with socket.create_connection((host, port), timeout=1.5):
                return True
        except OSError:
            pass
    return False


def helper(*args, timeout=30):
    try:
        r = subprocess.run(["sudo", "-n", HELPER, *args], capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "délai dépassé"
    msg = (r.stdout + r.stderr).strip().splitlines()
    return r.returncode == 0, (msg[-1] if msg else "")


def helper_installed():
    return os.path.exists(HELPER) and helper("check", timeout=5)[0]


def write_cred(path, user, password):
    domain = ""
    if "\\" in user:
        domain, user = user.split("\\", 1)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"username={user}\npassword={password}\n")
        if domain:
            f.write(f"domain={domain}\n")
    os.chmod(path, 0o600)


def read_cred(path):
    data = {}
    try:
        with open(path) as f:
            for line in f:
                k, _, v = line.rstrip("\n").partition("=")
                data[k] = v
    except OSError:
        pass
    user = data.get("username", "")
    if data.get("domain"):
        user = data["domain"] + "\\" + user
    return user, data.get("password", "")


def list_shares(host, fstype, user="", password=""):
    """Renvoie la liste des partages proposés par le serveur."""
    if fstype == "nfs":
        r = subprocess.run(["/sbin/showmount", "-e", "--no-headers", host],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "showmount a échoué")
        return [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]

    cmd = ["smbclient", "-g", "-L", f"//{host}"]
    tmp = None
    try:
        if user:
            tmp = os.path.join(CRED_DIR, f".scan-{uuid.uuid4().hex}")
            write_cred(tmp, user, password)
            cmd += ["-A", tmp]
        else:
            cmd += ["-N"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
    shares = []
    for ln in r.stdout.splitlines():
        parts = ln.split("|")
        if len(parts) >= 2 and parts[0] == "Disk" and not parts[1].endswith("$"):
            shares.append(parts[1])
    if not shares and r.returncode != 0:
        err = [l for l in (r.stdout + r.stderr).splitlines() if l.strip()]
        raise RuntimeError(err[-1] if err else "smbclient a échoué")
    return shares


def list_interfaces():
    """Interfaces Ethernet/Wi-Fi : [(nom, libellé)], celle de la route par défaut en tête."""
    try:
        addrs = json.loads(subprocess.run(["ip", "-j", "addr"], capture_output=True,
                                          text=True, timeout=5).stdout or "[]")
        routes = json.loads(subprocess.run(["ip", "-j", "route", "show", "default"],
                                           capture_output=True, text=True, timeout=5).stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        addrs, routes = [], []
    default = {r.get("dev") for r in routes}
    out = []
    for a in addrs:
        name = a.get("ifname", "")
        if a.get("link_type") != "ether":
            continue
        kind = "Wi-Fi" if os.path.isdir(f"/sys/class/net/{name}/wireless") else "Ethernet"
        ip4 = [i["local"] for i in a.get("addr_info", []) if i.get("family") == "inet"]
        state = a.get("operstate", "?")
        label = f"{name} — {kind} — {', '.join(ip4) or 'sans IPv4'} — {state}"
        out.append((name not in default, name, label))
    return [(n, l) for _, n, l in sorted(out)]


def dhcp_scan(iface, seconds):
    """Lance le scan via le helper. Renvoie (dict résultat, None) ou (None, erreur)."""
    ok, msg = helper("dhcp-scan", iface, str(seconds), timeout=seconds + 15)
    if ok:
        try:
            return json.loads(msg), None
        except ValueError:
            return None, f"réponse inattendue : {msg}"
    if "netmount-helper" in msg:   # ancien helper sans dhcp-scan : affiche son usage
        return None, "UPDATE"
    return None, msg or "échec du scan"


# --------------------------------------------------------------- config

def load_config():
    try:
        with open(CONF_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"entries": []}


def save_config(cfg):
    os.makedirs(CONF_DIR, mode=0o700, exist_ok=True)
    tmp = CONF_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONF_FILE)


def cred_path(entry):
    return os.path.join(CRED_DIR, entry["id"] + ".cred")


def do_mount(e):
    os.makedirs(resolve(e["mountpoint"]), exist_ok=True)
    args = ["mount", e["type"], e["host"], e["share"], resolve(e["mountpoint"])]
    if e["type"] == "cifs" and e.get("user"):
        args.append(cred_path(e))
    return helper(*args)


def do_umount(e, force=False):
    args = ["umount", resolve(e["mountpoint"])]
    if force:
        args.append("force")
    return helper(*args)


# --------------------------------------------------------------- surveillance

class Monitor(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.wake = threading.Event()
        self.stopping = False
        self.busy = threading.Lock()
        self.last_fail = {}   # id -> (heure, message)
        self.has_helper = False
        self.recheck_helper = True   # sudo -n journalise chaque appel : on ne vérifie pas à chaque cycle
        # Mode départ : tout est démonté proprement tant que les serveurs sont
        # encore là ; il se désactive tout seul quand plus aucun ne répond.
        self.leaving = False

    def run(self):
        while not self.stopping:
            self.cycle()
            self.wake.wait(INTERVAL)
            self.wake.clear()

    def cycle(self):
        with self.busy:
            if self.stopping:
                return
            entries = self.app.snapshot()
            if self.recheck_helper:
                self.has_helper = helper_installed()
                self.recheck_helper = False
            has_helper = self.has_helper
            results = {}
            for e in entries:
                try:
                    results[e["id"]] = self.handle(e, has_helper)
                except Exception as exc:  # ne jamais tuer la surveillance
                    results[e["id"]] = ("err", str(exc))
            if self.leaving and not any(s == "leave" for s, _ in results.values()):
                self.leaving = False   # serveurs disparus : on est parti, reprise auto
            GLib.idle_add(self.app.on_status, results, has_helper)

    def handle(self, e, has_helper):
        mounted = is_mounted(e["mountpoint"])
        if not e.get("enabled", True):
            if mounted and has_helper:
                do_umount(e)
            return ("off", "Désactivé")

        up = reachable(e["host"], PORTS[e["type"]])
        if self.leaving:
            if mounted:
                if not has_helper:
                    return ("leave", "Démontage impossible (autorisation)")
                ok, msg = do_umount(e)
                if not ok:
                    return ("leave", "Démontage échoué : " + msg)
            return ("leave", "Démonté — prêt à partir") if up else ("absent", "Serveur absent")
        if up and mounted:
            self.last_fail.pop(e["id"], None)
            return ("ok", "Monté")
        if up:
            if not has_helper:
                return ("err", "Autorisation non installée")
            fail = self.last_fail.get(e["id"])
            if fail and time.time() - fail[0] < RETRY_AFTER:
                return ("err", fail[1])
            ok, msg = do_mount(e)
            if ok:
                self.last_fail.pop(e["id"], None)
                return ("ok", "Monté")
            msg = "Échec du montage : " + msg
            self.last_fail[e["id"]] = (time.time(), msg)
            return ("err", msg)
        if mounted:
            if not has_helper:
                return ("err", "Serveur absent — démontage impossible (autorisation)")
            ok, msg = do_umount(e, force=True)
            return ("absent", "Serveur absent — démonté" if ok else "Démontage échoué : " + msg)
        return ("absent", "Serveur absent")


# --------------------------------------------------------------- fenêtre d'édition

class EntryDialog(Gtk.Dialog):
    def __init__(self, parent, entry=None):
        super().__init__(title="Partage réseau", transient_for=parent, modal=True)
        self.add_buttons("Annuler", Gtk.ResponseType.CANCEL, "Enregistrer", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_default_size(480, -1)
        self.entry = dict(entry) if entry else {"id": uuid.uuid4().hex[:8], "enabled": True}
        self.mp_touched = bool(entry)

        g = Gtk.Grid(column_spacing=10, row_spacing=8, margin=14)
        self.get_content_area().add(g)

        self.host = Gtk.Entry(text=self.entry.get("host", ""), placeholder_text="192.168.1.100")
        self.type = Gtk.ComboBoxText()
        for key, label in TYPE_LABELS:
            self.type.append(key, label)
        self.type.set_active_id(self.entry.get("type", "cifs"))
        self.type.connect("changed", self.on_type)

        user, password = ("", "")
        if entry and entry.get("user"):
            user, password = read_cred(cred_path(entry))
        self.user = Gtk.Entry(text=user, placeholder_text="vide = invité")
        self.password = Gtk.Entry(text=password, visibility=False)
        self.user_lbl = Gtk.Label(label="Utilisateur", xalign=0)
        self.pass_lbl = Gtk.Label(label="Mot de passe", xalign=0)

        self.scan_btn = Gtk.Button(label="Rechercher les partages")
        self.scan_btn.connect("clicked", self.on_scan)
        self.scan_msg = Gtk.Label(xalign=0, wrap=True)
        self.scan_msg.get_style_context().add_class("dim-label")

        self.share = Gtk.ComboBoxText.new_with_entry()
        self.share.get_child().set_text(self.entry.get("share", ""))
        self.share.get_child().set_placeholder_text("lancer la recherche ou saisir le nom")
        self.share.connect("changed", self.on_share)

        self.mp = Gtk.Entry(text=self.entry.get("mountpoint", ""), hexpand=True)
        self.mp.connect("changed", lambda *_: setattr(self, "mp_touched", True))
        mp_btn = Gtk.Button(label="Choisir…")
        mp_btn.connect("clicked", self.on_choose)
        mp_box = Gtk.Box(spacing=6)
        mp_box.pack_start(self.mp, True, True, 0)
        mp_box.pack_start(mp_btn, False, False, 0)

        rows = [
            (Gtk.Label(label="Serveur (IP)", xalign=0), self.host),
            (Gtk.Label(label="Type", xalign=0), self.type),
            (self.user_lbl, self.user),
            (self.pass_lbl, self.password),
            (None, self.scan_btn),
            (None, self.scan_msg),
            (Gtk.Label(label="Partage", xalign=0), self.share),
            (Gtk.Label(label="Monter dans", xalign=0), mp_box),
        ]
        for i, (lbl, w) in enumerate(rows):
            if lbl:
                g.attach(lbl, 0, i, 1, 1)
            w.set_hexpand(True)
            g.attach(w, 1, i, 1, 1)
        self.show_all()
        self.on_type()

    def on_type(self, *_):
        smb = self.type.get_active_id() == "cifs"
        for w in (self.user, self.password, self.user_lbl, self.pass_lbl):
            w.set_visible(smb)

    def on_share(self, *_):
        name = self.share.get_child().get_text().strip().strip("/")
        if name and not self.mp_touched:
            self.mp.set_text(os.path.join(HOME, "Reseau", os.path.basename(name)))
            self.mp_touched = False

    def on_choose(self, *_):
        dlg = Gtk.FileChooserDialog(title="Dossier de montage", transient_for=self,
                                    action=Gtk.FileChooserAction.SELECT_FOLDER)
        dlg.add_buttons("Annuler", Gtk.ResponseType.CANCEL, "Choisir", Gtk.ResponseType.OK)
        dlg.set_create_folders(True)
        cur = self.mp.get_text().strip()
        start = os.path.dirname(cur) if cur else HOME
        if os.path.isdir(start):
            dlg.set_current_folder(start)
        if dlg.run() == Gtk.ResponseType.OK:
            self.mp.set_text(dlg.get_filename())
        dlg.destroy()

    def on_scan(self, *_):
        host = self.host.get_text().strip()
        if not host:
            self.scan_msg.set_text("Entrer d'abord l'adresse IP du serveur.")
            return
        fstype = self.type.get_active_id()
        user, pw = self.user.get_text().strip(), self.password.get_text()
        self.scan_btn.set_sensitive(False)
        self.scan_msg.set_text("Recherche en cours…")

        def work():
            if not reachable(host, PORTS[fstype]):
                res = (None, f"{host} ne répond pas (port {PORTS[fstype]}).")
            else:
                try:
                    shares = list_shares(host, fstype, user, pw)
                    res = (shares, f"{len(shares)} partage(s) trouvé(s)." if shares
                           else "Aucun partage visible (identifiants nécessaires ?).")
                except Exception as exc:
                    res = (None, f"Erreur : {exc}")
            GLib.idle_add(self.scan_done, *res)

        threading.Thread(target=work, daemon=True).start()

    def scan_done(self, shares, msg):
        self.scan_btn.set_sensitive(True)
        self.scan_msg.set_text(msg)
        if shares:
            current = self.share.get_child().get_text()
            self.share.remove_all()
            for s in shares:
                self.share.append_text(s)
            if current in shares:
                self.share.set_active(shares.index(current))
            else:
                self.share.set_active(0)
            self.share.popup()

    def result(self):
        """Valide et renvoie (entrée, utilisateur, mot de passe) ou un message d'erreur."""
        host = self.host.get_text().strip()
        share = self.share.get_child().get_text().strip()
        mp = self.mp.get_text().strip()
        fstype = self.type.get_active_id()
        if not host:
            return "Adresse du serveur manquante."
        if not share:
            return "Choisir un partage."
        if fstype == "cifs":
            share = share.strip("/")
        elif not share.startswith("/"):
            share = "/" + share
        if not mp:
            return "Choisir le dossier de montage."
        mp = resolve(mp)
        if not mp.startswith(resolve(HOME) + "/"):
            return "Le dossier de montage doit être dans votre dossier personnel."
        e = dict(self.entry, host=host, type=fstype, share=share, mountpoint=mp)
        user = self.user.get_text().strip() if fstype == "cifs" else ""
        e["user"] = bool(user)
        return e, user, self.password.get_text()


# --------------------------------------------------------------- scan DHCP

class DhcpDialog(Gtk.Dialog):
    """Cherche les serveurs DHCP d'un réseau : plus d'une offre = serveur en double."""

    def __init__(self, app):
        super().__init__(title="Scan DHCP", transient_for=app.win, modal=True)
        self.app = app
        self.add_buttons("Fermer", Gtk.ResponseType.CLOSE)
        self.set_default_size(560, 420)

        g = Gtk.Grid(column_spacing=10, row_spacing=8, margin=14)
        self.get_content_area().pack_start(g, True, True, 0)

        self.iface = Gtk.ComboBoxText(hexpand=True)
        reload_btn = Gtk.Button.new_from_icon_name("view-refresh", Gtk.IconSize.BUTTON)
        reload_btn.set_tooltip_text("Recharger la liste des interfaces")
        reload_btn.connect("clicked", lambda *_: self.load_ifaces())
        if_box = Gtk.Box(spacing=6)
        if_box.pack_start(self.iface, True, True, 0)
        if_box.pack_start(reload_btn, False, False, 0)

        self.secs = Gtk.SpinButton.new_with_range(3, 30, 1)
        self.secs.set_value(10)
        self.scan_btn = Gtk.Button(label="Lancer le scan")
        self.scan_btn.connect("clicked", self.on_scan)
        self.update_btn = Gtk.Button(label="Mettre à jour l'autorisation (mot de passe admin)")
        self.update_btn.connect("clicked", lambda *_: self.app.on_install(None, None))
        self.update_btn.set_no_show_all(True)

        self.msg = Gtk.Label(xalign=0, wrap=True)
        self.out = Gtk.TextView(editable=False, monospace=True, cursor_visible=False)
        sw = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        sw.add(self.out)

        g.attach(Gtk.Label(label="Interface", xalign=0), 0, 0, 1, 1)
        g.attach(if_box, 1, 0, 1, 1)
        g.attach(Gtk.Label(label="Durée (s)", xalign=0), 0, 1, 1, 1)
        g.attach(self.secs, 1, 1, 1, 1)
        g.attach(self.scan_btn, 1, 2, 1, 1)
        g.attach(self.update_btn, 1, 3, 1, 1)
        g.attach(self.msg, 0, 4, 2, 1)
        g.attach(sw, 0, 5, 2, 1)
        self.load_ifaces()
        self.show_all()

    def load_ifaces(self):
        cur = self.iface.get_active_id()
        self.iface.remove_all()
        for name, label in list_interfaces():
            self.iface.append(name, label)
        if not (cur and self.iface.set_active_id(cur)):
            self.iface.set_active(0)

    def on_scan(self, *_):
        iface = self.iface.get_active_id()
        if not iface:
            self.msg.set_text("Aucune interface Ethernet/Wi-Fi trouvée.")
            return
        secs = self.secs.get_value_as_int()
        self.scan_btn.set_sensitive(False)
        self.update_btn.hide()
        self.out.get_buffer().set_text("")
        self.msg.set_text(f"Scan en cours sur {iface} ({secs} s)…")

        def work():
            res, err = dhcp_scan(iface, secs)
            GLib.idle_add(self.scan_done, res, err)

        threading.Thread(target=work, daemon=True).start()

    def scan_done(self, res, err):
        self.scan_btn.set_sensitive(True)
        if err == "UPDATE":
            self.msg.set_text("L'autorisation installée est trop ancienne pour le scan DHCP : "
                              "la mettre à jour puis relancer.")
            self.update_btn.show()
            return False
        if err:
            self.msg.set_text("Erreur : " + err)
            return False
        offers = res["offers"]
        n = len(offers)
        if n == 0:
            self.msg.set_markup("<b>Aucune réponse.</b> Réseau filtré (isolation Wi-Fi) ? "
                                "Réessayer en filaire.")
        elif n == 1:
            self.msg.set_markup(f"<b>Un seul serveur DHCP</b> ({offers[0]['server']}) : normal.")
        else:
            self.msg.set_markup(f"<b>⚠ {n} serveurs DHCP</b> répondent sur ce réseau !")
        lines = [f"{res['iface']}  MAC {res['mac']}  —  {res['sent']} DISCOVER, "
                 f"écoute {res['duration']} s", ""]
        for i, o in enumerate(offers, 1):
            lines.append(f"[{i}] serveur {o['server']}  (trame de {o['src_ip']} / {o['src_mac']})")
            lines.append(f"    IP proposée : {o['offered']}")
            for key, label in (("mask", "masque"), ("router", "passerelle"), ("dns", "DNS")):
                if o[key]:
                    lines.append(f"    {label:<11} : {', '.join(o[key])}")
            if o["lease"] is not None:
                lines.append(f"    bail        : {o['lease']} s")
            lines.append("")
        self.out.get_buffer().set_text("\n".join(lines))
        return False


# --------------------------------------------------------------- fenêtre principale

class App(Gtk.Application):
    def __init__(self, start_hidden):
        super().__init__(application_id=APP_ID)
        self.start_hidden = start_hidden
        self.cfg = load_config()
        self.cfg_lock = threading.Lock()
        self.status = {}
        self.win = None
        self.monitor = None
        self.tray = None

    # --- config partagée avec le thread de surveillance
    def snapshot(self):
        with self.cfg_lock:
            return [dict(e) for e in self.cfg["entries"]]

    def save(self):
        with self.cfg_lock:
            save_config(self.cfg)
        self.refresh()
        if self.monitor:
            self.monitor.wake.set()

    # --- cycle de vie
    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.install_launcher()
        self.build_window()
        self.build_tray()
        self.monitor = Monitor(self)
        self.monitor.start()

    def do_activate(self):
        if self.start_hidden and self.tray:
            self.start_hidden = False
            return
        self.win.show_all()
        self.infobar.set_visible(not self.helper_ok)
        self.win.present()

    def quit_all(self, *_):
        """Quitter = démonter tout ce qu'on a monté, pour ne rien laisser de mort derrière."""
        self.monitor.stopping = True
        self.monitor.wake.set()
        with self.monitor.busy:
            for e in self.snapshot():
                if is_mounted(e["mountpoint"]):
                    do_umount(e, force=not reachable(e["host"], PORTS[e["type"]], tries=1))
        self.quit()

    # --- interface
    def build_window(self):
        self.win = Gtk.ApplicationWindow(application=self, title="Partages réseau")
        self.win.set_default_size(760, 320)
        self.win.set_icon_name("folder-remote")
        self.win.connect("delete-event", self.on_close)
        self.helper_ok = True

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=10)
        self.win.add(box)

        self.infobar = Gtk.InfoBar(message_type=Gtk.MessageType.WARNING)
        self.infobar.get_content_area().add(Gtk.Label(
            label="L'autorisation de montage n'est pas installée : "
                  "rien ne peut être monté pour l'instant.", wrap=True, xalign=0))
        self.infobar.add_button("Installer (mot de passe admin)", 1)
        self.infobar.connect("response", self.on_install)
        box.pack_start(self.infobar, False, False, 0)

        # actif, id, serveur, partage, dossier, état, icône
        self.store = Gtk.ListStore(bool, str, str, str, str, str, str)
        self.view = Gtk.TreeView(model=self.store)
        self.view.connect("row-activated", lambda *_: self.on_edit())

        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self.on_toggle)
        self.view.append_column(Gtk.TreeViewColumn("Actif", toggle, active=0))
        icon_col = Gtk.TreeViewColumn("État")
        pix = Gtk.CellRendererPixbuf()
        txt = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        icon_col.pack_start(pix, False)
        icon_col.pack_start(txt, True)
        icon_col.add_attribute(pix, "icon-name", 6)
        icon_col.add_attribute(txt, "text", 5)
        icon_col.set_expand(True)
        for title, col in (("Serveur", 2), ("Partage", 3), ("Dossier", 4)):
            c = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=col)
            c.set_resizable(True)
            self.view.append_column(c)
        self.view.append_column(icon_col)

        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.add(self.view)
        box.pack_start(sw, True, True, 0)

        btns = Gtk.Box(spacing=6)
        for label, cb in (("Ajouter", self.on_add), ("Modifier", self.on_edit),
                          ("Supprimer", self.on_remove), ("Ouvrir le dossier", self.on_open),
                          ("Vérifier maintenant", lambda *_: self.monitor.wake.set()),
                          ("Scan DHCP…", self.on_dhcp)):
            b = Gtk.Button(label=label)
            b.connect("clicked", cb)
            btns.pack_start(b, False, False, 0)
        self.leave_btn = Gtk.Button()
        self.leave_btn.connect("clicked", self.on_leave)
        btns.pack_end(self.leave_btn, False, False, 0)
        quit_btn = Gtk.Button(label="Quitter")
        quit_btn.set_tooltip_text("Démonte tous les partages et ferme le programme")
        quit_btn.connect("clicked", self.quit_all)
        btns.pack_end(quit_btn, False, False, 0)
        box.pack_start(btns, False, False, 0)

        self.leave_msg = Gtk.Label(xalign=0, wrap=True)
        self.leave_msg.set_no_show_all(True)
        box.pack_start(self.leave_msg, False, False, 0)

        self.autostart = Gtk.CheckButton(label="Lancer automatiquement à l'ouverture de session")
        self.autostart.set_active(os.path.exists(AUTOSTART))
        self.autostart.connect("toggled", self.on_autostart)
        box.pack_start(self.autostart, False, False, 0)
        self.update_leave()
        self.refresh()

    def build_tray(self):
        try:
            self.tray = Gtk.StatusIcon.new_from_icon_name("folder-remote")
        except Exception:
            self.tray = None
            return
        self.tray.set_tooltip_text("Partages réseau")
        self.tray.connect("activate", lambda *_: self.toggle_window())
        menu = Gtk.Menu()
        for label, cb in (("Afficher", lambda *_: self.show_window()),
                          (None, self.on_leave),
                          ("Vérifier maintenant", lambda *_: self.monitor.wake.set()),
                          ("Quitter (démonte tout)", self.quit_all)):
            it = Gtk.MenuItem(label=label or "")
            it.connect("activate", cb)
            menu.append(it)
            if label is None:
                self.leave_item = it
        menu.show_all()
        self.update_leave()
        self.tray.connect("popup-menu", lambda icon, btn, t:
                          menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, btn, t))

    def show_window(self):
        self.win.show_all()
        self.infobar.set_visible(not self.helper_ok)
        self.win.present()

    def toggle_window(self):
        if self.win.get_visible():
            self.win.hide()
        else:
            self.show_window()

    def on_close(self, *_):
        if self.tray and self.tray.is_embedded():
            self.win.hide()     # continue de surveiller depuis la zone de notification
            return True
        self.quit_all()
        return True

    def refresh(self):
        sel = self.selected_id()
        self.store.clear()
        icons = {"ok": "emblem-default", "absent": "network-offline",
                 "err": "dialog-warning", "off": "media-playback-pause",
                 "leave": "media-eject"}
        for e in self.snapshot():
            st, msg = self.status.get(e["id"], ("", "…"))
            kind = "SMB" if e["type"] == "cifs" else "NFS"
            mp = e["mountpoint"].replace(HOME, "~", 1)
            self.store.append([e.get("enabled", True), e["id"], f'{e["host"]} ({kind})',
                               e["share"], mp, msg, icons.get(st, "content-loading")])
        if sel:
            for row in self.store:
                if row[1] == sel:
                    self.view.get_selection().select_iter(row.iter)

    def on_status(self, results, has_helper):
        self.status.update(results)
        self.helper_ok = has_helper
        if self.win.get_visible():
            self.infobar.set_visible(not has_helper)
        self.refresh()
        self.update_leave()
        if self.tray:
            n_ok = sum(1 for s, _ in results.values() if s == "ok")
            self.tray.set_tooltip_text(f"Partages réseau : {n_ok}/{len(results)} monté(s)")
        return False

    def on_leave(self, *_):
        m = self.monitor
        m.leaving = not m.leaving
        if m.leaving:
            for e in self.snapshot():
                if e.get("enabled", True) and is_mounted(e["mountpoint"]):
                    self.status[e["id"]] = ("leave", "Démontage en cours…")
            self.refresh()
        m.last_fail.clear()
        self.update_leave()
        m.wake.set()

    def update_leave(self):
        leaving = bool(self.monitor and self.monitor.leaving)
        label = "Reprendre le montage auto" if leaving else "Tout démonter (départ)"
        self.leave_btn.set_label(label)
        self.leave_btn.set_tooltip_text(
            "Remonte les partages dont le serveur est joignable" if leaving else
            "Démonte proprement tous les partages avant de partir, "
            "sans les remonter tant que les serveurs sont encore visibles")
        if getattr(self, "leave_item", None):
            self.leave_item.set_label(label)
        self.leave_msg.set_markup(
            "<b>Mode départ :</b> les partages sont démontés, vous pouvez partir. "
            "Le montage automatique reprendra tout seul quand les serveurs ne "
            "seront plus visibles." if leaving else "")
        self.leave_msg.set_visible(leaving)

    def selected_id(self):
        if not getattr(self, "view", None):
            return None
        model, it = self.view.get_selection().get_selected()
        return model[it][1] if it else None

    def find(self, eid):
        for e in self.cfg["entries"]:
            if e["id"] == eid:
                return e
        return None

    # --- actions
    def on_toggle(self, _r, path):
        eid = self.store[path][1]
        with self.cfg_lock:
            e = self.find(eid)
            e["enabled"] = not e.get("enabled", True)
        self.save()

    def on_add(self, *_):
        self.edit(None)

    def on_edit(self, *_):
        eid = self.selected_id()
        if eid:
            self.edit(dict(self.find(eid)))

    def edit(self, entry):
        dlg = EntryDialog(self.win, entry)
        while True:
            if dlg.run() != Gtk.ResponseType.OK:
                break
            res = dlg.result()
            if isinstance(res, str):
                self.error(dlg, res)
                continue
            new, user, pw = res
            if new["type"] == "cifs" and user:
                write_cred(cred_path(new), user, pw)
            elif os.path.exists(cred_path(new)):
                os.unlink(cred_path(new))
            with self.monitor.busy:
                if entry and is_mounted(entry["mountpoint"]):
                    do_umount(entry, force=True)
            with self.cfg_lock:
                self.cfg["entries"] = [x for x in self.cfg["entries"] if x["id"] != new["id"]]
                self.cfg["entries"].append(new)
                self.status.pop(new["id"], None)
                self.monitor.last_fail.pop(new["id"], None)
            self.save()
            break
        dlg.destroy()

    def on_remove(self, *_):
        eid = self.selected_id()
        if not eid:
            return
        e = self.find(eid)
        d = Gtk.MessageDialog(transient_for=self.win, modal=True,
                              message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.YES_NO,
                              text=f'Supprimer le partage {e["host"]} : {e["share"]} ?')
        d.format_secondary_text("Il sera démonté. Le dossier de montage (vide) est conservé.")
        ok = d.run() == Gtk.ResponseType.YES
        d.destroy()
        if not ok:
            return
        with self.monitor.busy:
            if is_mounted(e["mountpoint"]):
                do_umount(e, force=True)
        if os.path.exists(cred_path(e)):
            os.unlink(cred_path(e))
        with self.cfg_lock:
            self.cfg["entries"] = [x for x in self.cfg["entries"] if x["id"] != eid]
        self.save()

    def on_open(self, *_):
        eid = self.selected_id()
        if eid:
            e = self.find(eid)
            if not is_mounted(e["mountpoint"]):
                self.error(self.win, "Ce partage n'est pas monté actuellement.")
                return
            subprocess.Popen(["xdg-open", e["mountpoint"]])

    def on_dhcp(self, *_):
        dlg = DhcpDialog(self)
        dlg.run()
        dlg.destroy()

    def on_install(self, _bar, _resp):
        script = os.path.join(HERE, "install.sh")
        r = subprocess.run(["pkexec", "/bin/bash", script, os.environ.get("USER", "")],
                           capture_output=True, text=True)
        if r.returncode != 0:
            self.error(self.win, "Installation échouée ou annulée.\n\n"
                       + (r.stderr.strip() or r.stdout.strip())
                       + f"\n\nÀ défaut, dans un terminal :\nsudo {script}")
        self.monitor.recheck_helper = True
        self.monitor.wake.set()

    def on_autostart(self, btn):
        if btn.get_active():
            self.write_desktop(AUTOSTART, extra="--tray")
        elif os.path.exists(AUTOSTART):
            os.unlink(AUTOSTART)

    def install_launcher(self):
        self.write_desktop(LAUNCHER)

    @staticmethod
    def write_desktop(path, extra=""):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        exe = f'python3 "{os.path.join(HERE, "netmount.py")}" {extra}'.strip()
        with open(path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Partages réseau\n"
                    "Comment=Monte et démonte automatiquement les partages réseau\n"
                    f"Exec={exe}\nIcon=folder-remote\nTerminal=false\n"
                    "Categories=System;Network;\n")

    @staticmethod
    def error(parent, msg):
        d = Gtk.MessageDialog(transient_for=parent, modal=True,
                              message_type=Gtk.MessageType.WARNING,
                              buttons=Gtk.ButtonsType.OK, text=msg)
        d.run()
        d.destroy()


def main():
    hidden = "--tray" in sys.argv
    app = App(hidden)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
