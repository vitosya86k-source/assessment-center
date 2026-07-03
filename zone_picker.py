"""zone_picker.py — обвести плитку участника мышью ОДИН раз; печатает 'x,y,w,h' в stdout.

Используется start_ac_live.sh: чтобы залочить демон + эмоции на нужном человеке и
авто-выбор не прыгал между лицами (Иван/Руслан/середина). Запускать в venv с cv2+mss.

  ./venv_new/bin/python zone_picker.py        # печатает зону или ничего (если отмена)
"""
import sys

import cv2
import numpy as np
import mss


def select_zone(sct, monitor):
    """Полный экран → ручной выбор плитки мышью (namedWindow + callback)."""
    full = np.ascontiguousarray(np.array(sct.grab(monitor))[:, :, :3])
    H, W = full.shape[:2]
    scale = min(1.0, 1280.0 / W)
    disp = cv2.resize(full, (int(W * scale), int(H * scale))) if scale < 1 else full
    win = "Obvedi uchastnika -> ENTER"
    cv2.namedWindow(win, cv2.WINDOW_AUTOSIZE)
    cv2.imshow(win, disp)
    cv2.waitKey(1)
    st = {"p0": None, "p1": None}

    def cb(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            st["p0"], st["p1"] = (x, y), (x, y)
        elif event == cv2.EVENT_MOUSEMOVE and st["p0"] is not None:
            st["p1"] = (x, y)
        elif event == cv2.EVENT_LBUTTONUP and st["p0"] is not None:
            st["p1"] = (x, y)

    cv2.setMouseCallback(win, cb)
    zone = None
    while True:
        vis = disp.copy()
        if st["p0"] and st["p1"]:
            cv2.rectangle(vis, st["p0"], st["p1"], (0, 255, 0), 2)
        for col, th in (((0, 0, 0), 4), ((0, 255, 0), 1)):
            cv2.putText(vis, "obvedi uchastnika myshju | ENTER - ok | c - otmena", (10, 26),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, col, th, cv2.LINE_AA)
        cv2.imshow(win, vis)
        k = cv2.waitKey(20) & 0xFF
        if k in (13, 10, 32):
            if st["p0"] and st["p1"]:
                (x0, y0), (x1, y1) = st["p0"], st["p1"]
                x, y = min(x0, x1), min(y0, y1)
                w, h = abs(x1 - x0), abs(y1 - y0)
                if w >= 10 and h >= 10:
                    inv = 1.0 / scale
                    zone = (int(x * inv), int(y * inv), int(w * inv), int(h * inv))
            break
        if k in (ord("c"), 27):
            break
    cv2.destroyWindow(win)
    cv2.waitKey(1)
    return zone


def main():
    with mss.mss() as sct:
        mon = sct.monitors[1]
        z = select_zone(sct, mon)
    if z:
        sys.stdout.write(f"{z[0]},{z[1]},{z[2]},{z[3]}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
