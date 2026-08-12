#!/usr/bin/env python3
"""Capture UI screenshots via Selenium + geckodriver for the user manual."""

from __future__ import print_function

import os
import sys
import time

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(SCRIPT_DIR, "images")
BASE = "http://127.0.0.1:8765"
GECKODRIVER = "/tmp/gd-bin/geckodriver"


def log(msg):
    print(msg, flush=True)


def wait_boot(driver, timeout_s):
    WebDriverWait(driver, timeout_s).until(
        lambda d: d.execute_script(
            "var b=document.getElementById('app-boot'); return !b || !!b.hidden;"
        )
    )


def dismiss_overlays(driver):
    driver.execute_script(
        "var d=document.querySelector('#ksq-dialog');"
        "if(d) d.hidden=true;"
        "var o=document.querySelector('.dialog-overlay');"
        "if(o) o.hidden=true;"
        "var m=document.querySelector('#dash-confirm-modal');"
        "if(m) m.hidden=true;"
    )


def capture_view(driver, view, filename):
    url = "%s/?view=%s" % (BASE, view)
    log("navigate %s" % url)
    driver.get(url)
    wait_boot(driver, 30)
    time.sleep(1.0)
    dismiss_overlays(driver)
    path = os.path.join(OUT, filename)
    driver.save_screenshot(path)
    log("saved %s (%d bytes)" % (filename, os.path.getsize(path)))


def main():
    if not os.path.isfile(GECKODRIVER):
        raise RuntimeError("missing geckodriver at %s" % GECKODRIVER)
    os.makedirs(OUT, exist_ok=True)

    options = Options()
    options.add_argument("-headless")
    options.add_argument("--width=1440")
    options.add_argument("--height=900")
    options.set_preference("devtools.jsonview.enabled", False)

    service = Service(executable_path=GECKODRIVER)
    log("starting firefox via geckodriver")
    driver = webdriver.Firefox(service=service, options=options)
    try:
        driver.set_window_size(1440, 900)
        views = [
            ("dashboard", "01-dashboard.png"),
            ("load", "02-load.png"),
            ("query", "03-query.png"),
            ("order", "04-order.png"),
            ("test-order", "05-test-order.png"),
            ("logs", "06-logs.png"),
            ("settings", "07-settings.png"),
        ]
        for view, name in views:
            capture_view(driver, view, name)

        # Expand settings folds for a richer settings shot
        log("capture settings expanded")
        driver.get("%s/?view=settings" % BASE)
        wait_boot(driver, 30)
        time.sleep(0.8)
        for toggle in driver.find_elements(By.CSS_SELECTOR, "[data-fold-toggle]"):
            try:
                expanded = toggle.get_attribute("aria-expanded")
                if expanded != "true":
                    toggle.click()
                    time.sleep(0.2)
            except Exception:
                pass
        dismiss_overlays(driver)
        path = os.path.join(OUT, "07-settings.png")
        driver.save_screenshot(path)
        log("saved 07-settings.png expanded (%d bytes)" % os.path.getsize(path))

        # Loading splash: navigate and screenshot before boot finishes if possible
        log("capture loading splash")
        driver.get("%s/?view=dashboard" % BASE)
        try:
            WebDriverWait(driver, 2).until(
                EC.presence_of_element_located((By.ID, "app-boot"))
            )
            time.sleep(0.05)
            driver.save_screenshot(os.path.join(OUT, "00-loading.png"))
            log(
                "saved 00-loading.png (%d bytes)"
                % os.path.getsize(os.path.join(OUT, "00-loading.png"))
            )
        except Exception as exc:
            log("loading splash skipped: %s" % exc)

        log("done")
    finally:
        driver.quit()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log("ERROR: %s" % exc)
        sys.exit(1)
