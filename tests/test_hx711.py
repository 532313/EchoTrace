"""Compile the exact patched kernel callback with a controlled read-error stub."""

import json, shutil, subprocess, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DriverTests(unittest.TestCase):
    def test_real_callback_error_unlock_and_timestamp_scan(self):
        compiler = shutil.which("cc")
        if not compiler:
            self.skipTest("C compiler unavailable")
        function = json.loads((ROOT / "bsp/rv1106/hx711-baseline.json").read_text())[
            "patched_function"
        ]
        shim = """
#include <assert.h>
#include <stdbool.h>
#include <string.h>
#include <stdint.h>
typedef int irqreturn_t;
#define IRQ_HANDLED 1
#define IIO_VOLTAGE 1
struct channel { int channel,type; };
struct hx711_data { int lock; uint32_t buffer[4]; };
struct iio_dev {int masklength; unsigned long* active_scan_mask; struct channel* channels; void* trig; struct hx711_data data;};
struct iio_poll_func {struct iio_dev* indio_dev; long long timestamp;};
static int pushes,notifications,reads,fail,locked;
static void* iio_priv(struct iio_dev* d){return &d->data;}
static void mutex_lock(int* l){assert(!locked);locked=1;}
static void mutex_unlock(int* l){assert(locked);locked=0;}
static int test_bit(int i,unsigned long* m){return ((*m)>>i)&1;}
static int hx711_reset_read(struct hx711_data* d,int channel){reads++;assert(channel<2);return fail ? -110 : 123456;}
static void iio_push_to_buffers_with_timestamp(struct iio_dev* d,void* b,long long t){assert(locked);assert(((uint32_t*)b)[0]==123456);pushes++;}
static void iio_trigger_notify_done(void* t){assert(!locked);notifications++;}
"""
        main = """
int main(void){struct channel channels[]={{0,IIO_VOLTAGE},{1,IIO_VOLTAGE},{2,99}};unsigned long mask=5;struct iio_dev d={3,&mask,channels,0,{0,{0}}};struct iio_poll_func p={&d,123};
 hx711_trigger(0,&p);assert(pushes==1&&notifications==1&&reads==1&&!locked);
 fail=1;hx711_trigger(0,&p);assert(pushes==1&&notifications==2&&reads==2&&!locked);
 fail=0;mask=7;hx711_trigger(0,&p);assert(pushes==2&&notifications==3&&reads==4&&!locked);return 0;}
"""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "driver.c"
            exe = Path(tmp) / "driver"
            source.write_text(shim + function + main)
            subprocess.run(
                [compiler, "-std=c99", "-o", str(exe), str(source)],
                check=True,
                capture_output=True,
            )
            subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    unittest.main()
