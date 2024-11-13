#!/usr/bin/env python3
# Copyright lowRISC contributors (OpenTitan project).
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0
r"""Top Module Generator
"""
import argparse
import logging as log
import shutil
import sys
import tempfile
from collections import OrderedDict
from copy import deepcopy
from itertools import chain
from pathlib import Path
from typing import Dict, List

import hjson
import tlgen
import version_file
from ipgen import (IpBlockRenderer, IpConfig, IpDescriptionOnlyRenderer,
                   IpTemplate, TemplateRenderError)
from mako import exceptions
from mako.template import Template
from reggen import access, gen_rtl, gen_sec_cm_testplan, window
from reggen.countermeasure import CounterMeasure
from reggen.inter_signal import InterSignal
from reggen.ip_block import IpBlock
from reggen.params import ReggenParams
from reggen.lib import check_list
from topgen import get_hjsonobj_xbars
from topgen import intermodule as im
from topgen import lib as lib
from topgen import merge_top, search_ips, secure_prng, validate_top
from topgen.c_test import TopGenCTest
from topgen.clocks import Clocks, ClockSignal
from topgen.gen_dv import gen_dv
from topgen.gen_top_docs import gen_top_docs
from topgen.merge import connect_clocks, create_alert_lpgs, extract_clocks
from topgen.resets import Resets
from topgen.rust import TopGenRust
from topgen.top import Top

# Common header for generated files
warnhdr = """//
// ------------------- W A R N I N G: A U T O - G E N E R A T E D   C O D E !! -------------------//
// PLEASE DO NOT HAND-EDIT THIS FILE. IT HAS BEEN AUTO-GENERATED WITH THE FOLLOWING COMMAND:
"""
genhdr = """// Copyright lowRISC contributors (OpenTitan project).
// Licensed under the Apache License, Version 2.0, see LICENSE for details.
// SPDX-License-Identifier: Apache-2.0
""" + warnhdr

GENCMD = ("// util/topgen.py -t hw/{top_name}/data/{top_name}.hjson\n"
          "// -o hw/{top_name}")

SRCTREE_TOP = Path(__file__).parents[1].resolve()

TOPGEN_TEMPLATE_PATH = SRCTREE_TOP / "util" / "topgen" / "templates"


def ipgen_render(template_name: str, topname: str, params: Dict[str, object],
                 out_path: Path) -> None:
    """ Render an IP template for a specific toplevel using ipgen.

    The generated IP block is placed in the "ip_autogen" directory of the
    toplevel.

    Aborts the program execution in case of an error.
    """
    module_name = params.get("module_instance_name", template_name)
    top_name = f"top_{topname}"
    instance_name = f"{top_name}_{module_name}"
    ip_template = IpTemplate.from_template_path(SRCTREE_TOP / "hw" /
                                                "ip_templates" / template_name)

    params.update({"topname": topname})
    try:
        ip_config = IpConfig(ip_template.params, instance_name, params)
    except ValueError as e:
        log.error(f"Unable to render IP template {template_name!r}: {str(e)}")
        sys.exit(1)

    try:
        renderer = IpBlockRenderer(ip_template, ip_config)
        renderer.render(out_path / "ip_autogen" / module_name,
                        overwrite_output_dir=True)
    except TemplateRenderError as e:
        log.error(e.verbose_str())
        sys.exit(1)


def generate_top(top: Dict[str, object], name_to_block: Dict[str, IpBlock],
                 tpl_filename: str, **kwargs: Dict[str, object]) -> None:
    top_tpl = Template(filename=tpl_filename)

    try:
        return top_tpl.render(top=top, name_to_block=name_to_block, **kwargs)
    except:  # noqa: E722
        log.error(exceptions.text_error_template().render())
        return ""


def generate_xbars(top: Dict[str, object], out_path: Path) -> None:
    top_name = "top_" + top["name"]
    gencmd = (f"// util/topgen.py -t hw/{top_name}/data/{top_name}.hjson "
              f"-o hw/{top_name}/\n\n")

    for obj in top["xbar"]:
        objname = obj["name"]
        xbar_path = out_path / "ip" / f"xbar_{objname}" / "data" / "autogen"
        xbar_path.mkdir(parents=True, exist_ok=True)
        xbar = tlgen.validate(obj)
        xbar.ip_path = "/".join(["hw", top_name, "ip", "{dut}"])

        # Generate output of crossbar with complete fields
        xbar_hjson_path = xbar_path / f"xbar_{xbar.name}.gen.hjson"
        xbar_hjson_path.write_text(genhdr + gencmd +
                                   hjson.dumps(obj, for_json=True) + '\n')

        if not tlgen.elaborate(xbar):
            log.error("Elaboration failed." + repr(xbar))

        try:
            results = tlgen.generate(xbar, top_name)
        except:  # noqa: E722
            log.error(exceptions.text_error_template().render())

        ip_path = out_path / "ip" / f"xbar_{objname}"

        for filename, filecontent in results:
            filepath = ip_path / filename
            filepath.parent.mkdir(parents=True, exist_ok=True)
            with filepath.open(mode="w", encoding="UTF-8") as fout:
                fout.write(filecontent)

        dv_path = out_path / "ip" / f"xbar_{objname}" / "dv" / "autogen"
        dv_path.mkdir(parents=True, exist_ok=True)

        # generate testbench for xbar
        tlgen.generate_tb(xbar, dv_path, top_name)

        # Read back the comportable IP and amend to Xbar
        xbar_ipfile = ip_path / "data" / "autogen" / f"xbar_{objname}.hjson"
        with xbar_ipfile.open() as fxbar:
            xbar_ipobj = hjson.load(fxbar,
                                    use_decimal=True,
                                    object_pairs_hook=OrderedDict)

            r_inter_signal_list = check_list(
                xbar_ipobj.get("inter_signal_list", []),
                "inter_signal_list field")
            obj["inter_signal_list"] = [
                InterSignal.from_raw(
                    ReggenParams(),
                    "entry {} of the inter_signal_list field".format(idx + 1),
                    entry) for idx, entry in enumerate(r_inter_signal_list)
            ]


def generate_alert_handler(top: Dict[str, object], out_path: Path) -> None:
    log.info("Generating alert_handler with ipgen")
    topname = top["name"]

    # default values
    esc_cnt_dw = 32
    accu_cnt_dw = 16
    async_on = []
    # leave this constant
    n_classes = 4
    # low power groups
    n_lpg = 1
    lpg_map = []

    # Count number of alerts and LPGs
    n_alerts = sum([x["width"] if "width" in x else 1 for x in top["alert"]])
    n_lpg = len(top["alert_lpgs"])
    n_lpg_width = n_lpg.bit_length()
    # format used to print out indices in binary format
    async_on_format = "1'b{:01b}"
    lpg_idx_format = str(n_lpg_width) + "'d{:d}"

    # Double check that all these values are greated than 0
    if esc_cnt_dw < 1:
        raise ValueError("esc_cnt_dw must be larger than 0")
    if accu_cnt_dw < 1:
        raise ValueError("accu_cnt_dw must be larger than 0")
    if n_lpg < 1:
        raise ValueError("n_lpg must be larger than 0")

    if n_alerts < 1:
        # set number of alerts to 1 such that the config is still valid
        # that input will be tied off
        n_alerts = 1
        log.warning("no alerts are defined in the system")
    else:
        async_on = []
        lpg_map = []
        for alert in top["alert"]:
            for k in range(alert["width"]):
                async_on.append(async_on_format.format(int(alert["async"])))
                lpg_map.append(lpg_idx_format.format(int(alert["lpg_idx"])))

    params = {
        "n_alerts": n_alerts,
        "esc_cnt_dw": esc_cnt_dw,
        "accu_cnt_dw": accu_cnt_dw,
        "async_on": async_on,
        "n_classes": n_classes,
        "n_lpg": n_lpg,
        "lpg_map": lpg_map,
    }

    ipgen_render("alert_handler", topname, params, out_path)


def generate_plic(top: Dict[str, object], out_path: Path) -> None:
    log.info("Generating rv_plic with ipgen")
    topname = top["name"]
    params = {}

    # Count number of interrupts
    # Interrupt source 0 is tied to 0 to conform RISC-V PLIC spec.
    # So, total number of interrupts are the number of entries in the list + 1
    params["src"] = sum(
        [x["width"] if "width" in x else 1 for x in top["interrupt"]]) + 1

    # Target and priority: Currently fixed
    params["target"] = int(top["num_cores"], 0) if "num_cores" in top else 1
    params["prio"] = 3

    ipgen_render("rv_plic", topname, params, out_path)


# TODO(lowrisc/opentitan#8440): For templated IPs we have to search
# both the source and destination RTL directories, since not all files
# are copied over. This is a workaround which can be removed once all
# generated IPs have transitioned to IPgen.
def generate_regfile_from_path(hjson_path: Path,
                               generated_rtl_path: Path,
                               original_rtl_path: Path = None) -> None:
    """Generate RTL register file from path and check countermeasure labels"""
    obj = IpBlock.from_path(str(hjson_path), [])

    # If this block has countermeasures, we grep for RTL annotations in
    # all .sv implementation files and check whether they match up
    # with what is defined inside the Hjson.
    sv_files = generated_rtl_path.glob("*.sv")
    if original_rtl_path is not None:
        sv_files = chain(sv_files, original_rtl_path.glob("*.sv"))
    rtl_names = CounterMeasure.search_rtl_files(sv_files)
    obj.check_cm_annotations(rtl_names, str(hjson_path))
    gen_rtl.gen_rtl(obj, str(generated_rtl_path))
    if gen_sec_cm_testplan.gen_sec_cm_testplan(obj, hjson_path.parent):
        sys.exit(1)


def generate_pinmux(top: Dict[str, object], out_path: Path) -> None:
    log.info("Generating pinmux with ipgen")
    topname = top["name"]

    # Generation without pinmux and pinout configuration is not supported.
    assert "pinmux" in top
    assert "pinout" in top

    pinmux = top["pinmux"]

    # Get number of wakeup detectors
    if "num_wkup_detect" in pinmux:
        num_wkup_detect = pinmux["num_wkup_detect"]
    else:
        num_wkup_detect = 1

    if num_wkup_detect <= 0:
        # TODO: add support for no wakeup counter case
        log.error("Topgen does currently not support generation of a top " +
                  "without DIOs.")
        return

    if "wkup_cnt_width" in pinmux:
        wkup_cnt_width = pinmux["wkup_cnt_width"]
    else:
        wkup_cnt_width = 8

    if wkup_cnt_width <= 1:
        log.error("Wakeup counter width must be greater equal 2.")
        return

    # MIO Pads
    n_mio_pads = pinmux["io_counts"]["muxed"]["pads"]

    # Total inputs/outputs
    # Reuse the counts from the merge phase
    n_mio_periph_in = (pinmux["io_counts"]["muxed"]["inouts"] +
                       pinmux["io_counts"]["muxed"]["inputs"])
    n_mio_periph_out = (pinmux["io_counts"]["muxed"]["inouts"] +
                        pinmux["io_counts"]["muxed"]["outputs"])
    n_dio_periph_in = (pinmux["io_counts"]["dedicated"]["inouts"] +
                       pinmux["io_counts"]["dedicated"]["inputs"])
    n_dio_periph_out = (pinmux["io_counts"]["dedicated"]["inouts"] +
                        pinmux["io_counts"]["dedicated"]["outputs"])
    n_dio_pads = (pinmux["io_counts"]["dedicated"]["inouts"] +
                  pinmux["io_counts"]["dedicated"]["inputs"] +
                  pinmux["io_counts"]["dedicated"]["outputs"])

    # Generation with zero MIO/DIO pads is currently not supported.
    assert (n_mio_pads > 0)
    assert (n_dio_pads > 0)

    log.info("Generating pinmux with following info from hjson:")
    log.info("num_wkup_detect: %d" % num_wkup_detect)
    log.info("wkup_cnt_width:  %d" % wkup_cnt_width)
    log.info("n_mio_periph_in:  %d" % n_mio_periph_in)
    log.info("n_mio_periph_out: %d" % n_mio_periph_out)
    log.info("n_dio_periph_in:  %d" % n_dio_periph_in)
    log.info("n_dio_periph_out: %d" % n_dio_periph_out)
    log.info("n_dio_pads:       %d" % n_dio_pads)

    params = {
        "n_wkup_detect": num_wkup_detect,
        "wkup_cnt_width": wkup_cnt_width,
        "n_mio_pads": n_mio_pads,
        "n_mio_periph_in": n_mio_periph_in,
        "n_mio_periph_out": n_mio_periph_out,
        "n_dio_pads": n_dio_pads,
        "n_dio_periph_in": n_dio_periph_in,
        "n_dio_periph_out": n_dio_periph_out,
        "enable_usb_wakeup": pinmux['enable_usb_wakeup'],
        "enable_strap_sampling": pinmux['enable_strap_sampling']
    }

    ipgen_render("pinmux", topname, params, out_path)


# generate clkmgr with ipgen
def generate_clkmgr(topcfg: Dict[str, object], out_path: Path) -> None:
    log.info("Generating clkmgr with ipgen")
    topname = topcfg["name"]

    clocks = topcfg["clocks"]
    assert isinstance(clocks, Clocks)

    typed_clocks = clocks.typed_clocks()
    hint_names = typed_clocks.hint_names()

    typed_clks = OrderedDict({
        ty: {nm: {"src_name": sig.src.name, "endpoint_ip": sig.endpoints[0][0]}
             for nm, sig in mp.items() if isinstance(sig, ClockSignal)}
        for ty, mp in typed_clocks._asdict().items() if isinstance(mp, dict)})
    params = {
        "src_clks": OrderedDict({
            name: vars(obj) for name, obj in clocks.srcs.items()}),
        "derived_clks": OrderedDict({
            name: vars(obj) for name, obj in clocks.derived_srcs.items()}),
        "typed_clocks": OrderedDict({
            ty: d for ty, d in typed_clks.items() if d}),
        "hint_names": hint_names,
        "parent_child_clks": typed_clocks.parent_child_clks,
        "exported_clks": topcfg["exported_clks"],
        "number_of_clock_groups": len(clocks.groups)
    }

    ipgen_render("clkmgr", topname, params, out_path)


def generate_pwrmgr(top: Dict[str, object], out_path: Path) -> None:
    log.info("Generating pwrmgr with ipgen")
    topname = top["name"]

    # Count number of wakeups
    n_wkups = len(top["wakeups"])
    log.info("Found {} wakeup signals".format(n_wkups))

    # Count number of reset requests
    n_rstreqs = len(top["reset_requests"]["peripheral"])
    log.info("Found {} reset request signals".format(n_rstreqs))

    if n_wkups < 1:
        n_wkups = 1
        log.warning(
            "The design has no wakeup sources. Low power not supported.")

    if n_rstreqs < 1:
        n_rstreqs = 1
        log.warning("The design has no reset request sources. "
                    "Reset requests are not supported.")

    params = {
        "NumWkups": n_wkups,
        "Wkups": top["wakeups"],
        "rst_reqs": top["reset_requests"],
        "NumRstReqs": n_rstreqs,
        "wait_for_external_reset": top['power']['wait_for_external_reset']
    }

    ipgen_render("pwrmgr", topname, params, out_path)


def get_rst_ni(top: Dict[str, object]) -> object:
    rstmgrs = [m for m in top['module'] if m['type'] == 'rstmgr']
    return rstmgrs[0]["reset_connections"]


# generate rstmgr with ipgen
def generate_rstmgr(topcfg: Dict[str, object], out_path: Path) -> None:
    log.info("Generating rstmgr with ipgen")
    topname = topcfg["name"]

    # Parameters needed for generation
    reset_obj = topcfg["resets"]

    # The original resets dict is transformed to the reset class
    assert isinstance(reset_obj, Resets)

    # unique clocks
    clks = reset_obj.get_clocks()

    # resets sent to reset struct
    output_rsts = reset_obj.get_top_resets()

    # sw controlled resets
    sw_rsts = reset_obj.get_sw_resets()

    # rst_ni
    rst_ni = get_rst_ni(topcfg)

    # leaf resets
    leaf_rsts = reset_obj.get_generated_resets()

    # Number of reset requests
    n_rstreqs = len(topcfg["reset_requests"]["peripheral"])

    params = {
        "clks": clks,
        "reqs": topcfg["reset_requests"],
        "power_domains": topcfg["power"]["domains"],
        "num_rstreqs": n_rstreqs,
        "sw_rsts": sw_rsts,
        "output_rsts": output_rsts,
        "leaf_rsts": leaf_rsts,
        "rst_ni": rst_ni['rst_ni']['name'],
        "export_rsts": topcfg["exported_rsts"],
    }

    ipgen_render("rstmgr", topname, params, out_path)


# generate flash_ctrl with ipgen
def generate_flash(topcfg: Dict[str, object], out_path: Path) -> None:
    log.info("Generating flash_ctrl with ipgen")
    topname = topcfg["name"]

    # Parameters needed for generation
    flash_mems = [
        module for module in topcfg["module"] if module["type"] == "flash_ctrl"
    ]
    if len(flash_mems) > 1:
        log.error("This design does not currently support multiple flashes")
        return
    elif len(flash_mems) == 0:
        log.info("This design does not instantiate a flash controller")
        return

    params = vars(flash_mems[0]["memory"]["mem"]["config"])
    # Additional parameters not provided in the top config.
    params.update({
        "metadata_width": 12,
        "info_types": 3,
        "infos_per_bank": [10, 1, 2]
    })

    params.pop('base_addrs', None)
    ipgen_render("flash_ctrl", topname, params, out_path)


def generate_top_only(top_only_dict: Dict[str, bool], out_path: Path,
                      top_name: str, alt_hjson_path: str) -> None:
    log.info("Generating top only modules")

    for ip, reggen_only in top_only_dict.items():

        if reggen_only and alt_hjson_path is not None:
            hjson_dir = Path(alt_hjson_path)
        else:
            hjson_dir = (SRCTREE_TOP / "hw" / top_name / "ip" / ip / "data")

        hjson_path = hjson_dir / f"{ip}.hjson"

        genrtl_dir = out_path / "ip" / ip / "rtl"
        genrtl_dir.mkdir(parents=True, exist_ok=True)
        log.info("Generating top modules {}, hjson: {}, output: {}".format(
            ip, hjson_path, genrtl_dir))

        # Generate reg files
        generate_regfile_from_path(hjson_path, genrtl_dir)


def generate_top_ral(top: Dict[str, object], name_to_block: Dict[str, IpBlock],
                     dv_base_names: List[str], out_path: str):
    # construct top ral block
    regwidth = int(top["datawidth"])
    assert regwidth % 8 == 0
    addrsep = regwidth // 8

    # Generate a map from instance name to the block that it instantiates,
    # together with a map of interface addresses.
    inst_to_block = {}  # type: Dict[str, str]
    if_addrs = {}  # type: Dict[Tuple[str, Optional[str]], int]
    attrs = {}  # type: Dict[str, str]

    for module in top["module"]:
        inst_name = module["name"]
        block_name = module["type"]
        block = name_to_block[block_name]
        if "attr" in module:
            if module["attr"] not in [
                    "templated", "ipgen", "reggen_top", "reggen_only"
            ]:
                raise ValueError(
                    "Unsupported value for attr field of {}: {!r}".format(
                        inst_name, module["attr"]))
            attrs[inst_name] = module["attr"]

        inst_to_block[inst_name] = block_name
        for if_name in block.reg_blocks.keys():
            if_addr = {asid: int(addr, 0) for (asid, addr) in module["base_addrs"][if_name].items()}
            if_addrs[(inst_name, if_name)] = if_addr

    # Collect up the memories to add
    mems = []
    for item in list(top.get("memory", [])):
        mems.append(create_mem(item, addrsep, regwidth))

    # Top-level may override the mem setting. Store the new type to name_to_block
    # If no other instance uses the orignal type, delete it
    original_types = set()
    for module in top["module"]:
        if "memory" in module.keys() and len(module["memory"]) > 0:
            newtype = "{}_{}".format(module["type"], module["name"])
            assert newtype not in name_to_block

            block = deepcopy(name_to_block[module["type"]])
            name_to_block[newtype] = block
            inst_to_block[module["name"]] = newtype

            original_types.add(module["type"])

            for mem_name, item in module["memory"].items():
                assert block.reg_blocks[mem_name]
                assert len(block.reg_blocks[mem_name].windows) <= 1
                item["name"] = mem_name

                win = create_mem(item, addrsep, regwidth)
                if len(block.reg_blocks[mem_name].windows) > 0:
                    blk_win = block.reg_blocks[mem_name].windows[0]

                    # Top can only add new info for mem, shouldn't overwrite
                    # existing configuration
                    assert win.items == blk_win.items
                    assert win.byte_write == blk_win.byte_write
                    assert win.data_intg_passthru == blk_win.data_intg_passthru

                    block.reg_blocks[mem_name].windows[0] = win
                else:
                    block.reg_blocks[mem_name].windows.append(win)

    for t in original_types:
        if t not in inst_to_block.values():
            del name_to_block[t]

    addr_spaces = {addr_space["name"] for addr_space in top["addr_spaces"]}
    chip = Top(regwidth, addr_spaces, name_to_block, inst_to_block, if_addrs, mems, attrs)

    # generate the top ral model with template
    return gen_dv(chip, dv_base_names, str(out_path))


def create_mem(item, addrsep, regwidth):
    byte_write = ("byte_write" in item and
                  item["byte_write"].lower() == "true")
    data_intg_passthru = ("data_intg_passthru" in item and
                          item["data_intg_passthru"].lower() == "true")
    size_in_bytes = int(item["size"], 0)
    num_regs = size_in_bytes // addrsep
    swaccess = access.SWAccess("top-level memory", item.get("swaccess", "rw"))

    return window.Window(name=item["name"],
                         desc="(generated from top-level)",
                         unusual=False,
                         byte_write=byte_write,
                         data_intg_passthru=data_intg_passthru,
                         validbits=regwidth,
                         items=num_regs,
                         size_in_bytes=size_in_bytes,
                         offset=int(item.get("base_addr", "0"), 0),
                         swaccess=swaccess)


def generate_rust(topname, completecfg, name_to_block, out_path, version_stamp,
                  src_tree_top, topgen_template_path):
    # Template render helper
    def render_template(template_path: str, rendered_path: Path, **other_info):
        template_contents = generate_top(completecfg, name_to_block,
                                         str(template_path), **other_info)

        rendered_path.parent.mkdir(exist_ok=True, parents=True)
        with rendered_path.open(mode="w", encoding="UTF-8") as fout:
            fout.write(template_contents)

    # The Rust file needs some complex information, so we initialize this
    # object to store it.
    rs_helper = TopGenRust(completecfg, name_to_block, version_stamp)

    rust_files = [("toplevel_mod.rs.tpl", "mod.rs"),
                  ("toplevel.rs.tpl", f"top_{topname}.rs")]

    # Create Rust output directory
    rsformat_dir = out_path / "sw/autogen/chip/"
    rsformat_dir.mkdir(parents=True, exist_ok=True)

    # Generating Rust device description for external sw usage
    for (template, source) in rust_files:
        render_template(topgen_template_path / template,
                        rsformat_dir / source,
                        helper=rs_helper)

    # Generate Rust host-side files
    rsformat_dir = src_tree_top / "sw/host/opentitanlib/src/chip/autogen"
    rsformat_dir.mkdir(parents=True, exist_ok=True)
    render_template(topgen_template_path / "host_toplevel.rs.tpl",
                    rsformat_dir / f"{topname}.rs",
                    helper=rs_helper)


def _process_top(
        topcfg: Dict[str, object], args: argparse.Namespace, cfg_path: Path,
        out_path: Path, pass_idx: int
) -> (Dict[str, object], Dict[str, IpBlock], Dict[str, Path]):
    # Create generated list
    # These modules are generated through topgen
    templated_list = lib.get_templated_modules(topcfg)
    log.info("Templated list is {}".format(templated_list))

    ipgen_list = lib.get_ipgen_modules(topcfg)
    log.info("Ip gen list is {}".format(ipgen_list))

    generated_list = templated_list + ipgen_list

    # These modules are NOT generated but belong to a specific top
    # and therefore not part of "hw/ip"
    top_only_dict = {
        module["type"]: lib.is_reggen_only(module)
        for module in topcfg["module"] if lib.is_top_reggen(module)
    }
    log.info("Filtered dict is {}".format(top_only_dict))

    top_name = f"top_{topcfg['name']}"

    # Sweep the IP directory and gather the config files
    ip_dir = Path(__file__).parents[1] / "hw/ip"
    ips = search_ips(ip_dir)

    # exclude filtered IPs (to use ${top_name} one) and
    exclude_list = generated_list + list(top_only_dict.keys())
    ips = [x for x in ips if not x.parents[1].name in exclude_list]

    # Hack alert
    # Generate clkmgr.hjson here so that it can be included below
    # Unlike other generated hjsons, clkmgr thankfully does not require
    # ip.hjson information.  All the information is embedded within
    # the top hjson file
    topcfg["clocks"] = Clocks(topcfg["clocks"])
    extract_clocks(topcfg)
    generate_clkmgr(topcfg, out_path)

    # It may require two passes to check if the module is needed.
    # TODO: first run of topgen will fail due to the absent of rv_plic.
    # It needs to run up to amend_interrupt in merge_top function
    # then creates rv_plic.hjson then run xbar generation.
    hjson_dir = Path(args.topcfg).parent

    for ip in generated_list:
        # For modules that are generated prior to gathering, we need to take it from
        # the output path.  For modules not generated before, it may exist in a
        # pre-defined area already.
        log.info("Appending {}".format(ip))
        if ip in ipgen_list:
            ip_relpath = "ip_autogen"
            desc_file_relpath = "data"
        else:
            ip_relpath = "ip"
            desc_file_relpath = "data/autogen"

        if ip == "clkmgr" or (pass_idx > 0):
            ip_hjson = (Path(out_path) / ip_relpath / ip / desc_file_relpath /
                        f"{ip}.hjson")
        else:
            ip_hjson = (hjson_dir.parent / ip_relpath / ip /
                        desc_file_relpath / f"{ip}.hjson")
        ips.append(ip_hjson)

    for ip, reggen_only in top_only_dict.items():
        log.info("Appending {}".format(ip))

        if reggen_only and args.hjson_path:
            ip_hjson = Path(args.hjson_path) / f"{ip}.hjson"
        else:
            ip_hjson = hjson_dir.parent / f"ip/{ip}/data/{ip}.hjson"

        ips.append(ip_hjson)

    # load Hjson and pass validate from reggen
    name_to_hjson = {}  # type Dict[str, Path]
    try:
        ip_objs = []
        for ip_desc_file in ips:
            ip_name = ip_desc_file.stem
            # Skip if it is not in the module list
            if ip_name not in [ip["type"] for ip in topcfg["module"]]:
                log.info("Skip module %s as it isn't in the top module list" %
                         ip_name)
                continue

            # The auto-generated hjson might not yet exist. It will be created
            # later, see generate_{ip_name}() calls below. For the initial
            # validation, use the Hjson file with default values.
            # TODO: All of this is a rather ugly hack that we need to get rid
            # of as soon as we don't arbitrarily template IP description Hjson
            # files any more.
            if ip_name in generated_list and not ip_desc_file.is_file():
                if ip_name in ipgen_list:
                    log.info(
                        "To-be-auto-generated Hjson %s does not yet exist. "
                        "Falling back to the default configuration of template "
                        "%s for initial validation." % (ip_desc_file, ip_name))

                    tpl_path = SRCTREE_TOP / "hw/ip_templates" / ip_name
                    ip_template = IpTemplate.from_template_path(tpl_path)
                    ip_config = IpConfig(ip_template.params,
                                         f"{top_name}_{ip_name}")

                    try:
                        ip_desc = IpDescriptionOnlyRenderer(
                            ip_template, ip_config).render()
                    except TemplateRenderError as e:
                        log.error(e.verbose_str())
                        sys.exit(1)
                    name_to_hjson[ip_name.lower()] = tpl_path
                    s = "default description of IP template {}".format(ip_name)
                    ip_objs.append(IpBlock.from_text(ip_desc, [], s))
                else:
                    # TODO: Remove this block as soon as all IP templates use
                    # ipgen.
                    template_hjson_file = ip_dir / "{}/data/{}.hjson".format(
                        ip_name, ip_name)
                    log.info(
                        "To-be-auto-generated Hjson %s does not yet exist. "
                        "Falling back to Hjson description file %s shipped "
                        "with the IP template for initial validation." %
                        (ip_desc_file, template_hjson_file))
                    name_to_hjson[ip_name] = template_hjson_file
                    ip_objs.append(
                        IpBlock.from_path(str(template_hjson_file), []))
            else:
                name_to_hjson[ip_name] = ip_desc_file
                ip_objs.append(IpBlock.from_path(str(ip_desc_file), []))

    except ValueError:
        raise SystemExit(sys.exc_info()[1])

    name_to_block = {}  # type: Dict[str, IpBlock]
    for block in ip_objs:
        lblock = block.name.lower()
        assert lblock not in name_to_block
        name_to_block[lblock] = block

    # Read in alias files one-by-one, peek inside to figure out which IP block
    # they belong to and apply the alias file to that IP block.
    if args.alias_files:
        for alias in args.alias_files:
            with open(alias, 'r', encoding='utf-8') as handle:
                raw = hjson.loads(handle.read(), use_decimal=True)
                if 'alias_target' not in raw:
                    raise ValueError('Missing alias_target key '
                                     'in alias file {}.'.format(alias))
                alias_target = raw['alias_target'].lower()
                if alias_target not in name_to_block:
                    raise ValueError(
                        'Alias target {} is not defined.'.format(alias_target))
                where = 'alias file at {}'.format(alias)
                name_to_block[alias_target].alias_from_raw(False, raw, where)

    connect_clocks(topcfg, name_to_block)

    # Read the crossbars under the top directory
    xbar_objs = get_hjsonobj_xbars(hjson_dir)

    log.info("Detected crossbars: %s" %
             (", ".join([x["name"] for x in xbar_objs])))

    topcfg, error = validate_top(topcfg, ip_objs, xbar_objs)
    if error != 0:
        raise SystemExit("Error occured while validating top.hjson")

    completecfg = merge_top(topcfg, name_to_block, xbar_objs)

    # Generate flash controller and flash memory
    generate_flash(topcfg, out_path)

    # Generate PLIC
    if not args.no_plic and \
       not args.alert_handler_only and \
       not args.xbar_only:
        generate_plic(completecfg, out_path)
        if args.plic_only:
            sys.exit()

    # Create Alert Handler LPGs before
    # generating the Alert Handler
    create_alert_lpgs(topcfg, name_to_block)

    # Generate Alert Handler
    if not args.xbar_only:
        generate_alert_handler(completecfg, out_path)
        if args.alert_handler_only:
            sys.exit()

    # Generate Pinmux
    generate_pinmux(completecfg, out_path)

    # Generate Pwrmgr
    generate_pwrmgr(completecfg, out_path)

    # Generate rstmgr
    generate_rstmgr(completecfg, out_path)

    # Generate top only modules
    # These modules are not templated, but are not in hw/ip
    generate_top_only(top_only_dict, out_path, top_name, args.hjson_path)

    return completecfg, name_to_block, name_to_hjson


def _check_countermeasures(completecfg: Dict[str, object],
                           name_to_block: Dict[str, IpBlock],
                           name_to_hjson: Dict[str, Path]) -> bool:
    success = True
    for name, hjson_path in name_to_hjson.items():
        log.debug("name %s, hjson %s", name, hjson_path)
        ip_index = [m['type'] for m in completecfg['module']].index(name)
        # TODO(lowrisc/opentitan#8440): For templated IPs we have to search
        # both the source and destination RTL directories, since not all files
        # are copied over. This is a workaround which can be removed once all
        # generated IPs have transitioned to IPgen.
        if ('attr' in completecfg['module'][ip_index] and
                completecfg['module'][ip_index]['attr'] == 'templated'):
            ip_proper_rtl_path = hjson_path.parents[5] / 'ip' / name / 'rtl'
            sv_files = (hjson_path.parents[2] / 'rtl' / 'autogen').glob('*.sv')
            sv_files = set(sv_files)
            sv_names = set([f.name for f in sv_files])
            proper_rtl_files = {
                f
                for f in ip_proper_rtl_path.glob('*.sv')
                if f.name not in sv_names
            }
            sv_files = sv_files | proper_rtl_files
        else:
            sv_files = (hjson_path.parents[1] / 'rtl').glob('*.sv')
        rtl_names = CounterMeasure.search_rtl_files(sv_files)
        log.debug("Checking countermeasures for %s.", name)
        success &= name_to_block[name].check_cm_annotations(
            rtl_names, hjson_path.name)
    if success:
        log.info("All Hjson declared countermeasures are implemented in RTL.")
    else:
        log.error("Countermeasure checks failed.")
    return success


def main():
    parser = argparse.ArgumentParser(prog="topgen")
    parser.add_argument("--topcfg",
                        "-t",
                        required=True,
                        help="`top_{name}.hjson` file.")
    parser.add_argument(
        "--outdir",
        "-o",
        help="""Target TOP directory.
             Module is created under rtl/. (default: dir(topcfg)/..)
             """)  # yapf: disable
    parser.add_argument("--hjson-path",
                        help="""
          If defined, topgen uses supplied path to search for ip hjson.
          This applies only to ip's with the `reggen_only` attribute.
          If an hjson is located both in the conventional path and the alternate
          path, the alternate path has priority.
        """)
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose")
    parser.add_argument(
        '--version-stamp',
        type=Path,
        default=None,
        help=
        'If version stamping, the location of workspace version stamp file.')

    # Generator options: 'no' series. Cannot combine with 'only' series.
    parser.add_argument(
        "--no-top",
        action="store_true",
        help="If defined, topgen doesn't generate top_{name} RTLs.")
    parser.add_argument(
        "--no-xbar",
        action="store_true",
        help="If defined, topgen doesn't generate crossbar RTLs.")
    parser.add_argument(
        "--no-plic",
        action="store_true",
        help="If defined, topgen doesn't generate the interrup controller RTLs."
    )
    parser.add_argument("--no-rust",
                        action="store_true",
                        help="If defined, topgen doesn't generate Rust code.")

    # Generator options: 'only' series. cannot combined with 'no' series
    parser.add_argument(
        "--top-only",
        action="store_true",
        help="If defined, the tool generates top RTL only")  # yapf:disable
    parser.add_argument("--check-cm",
                        action="store_true",
                        help='''
        Check countermeasures.

        Check countermeasures of all modules in the top config. All
        countermeasures declared in the module's hjson file should
        be implemented in the RTL, and the RTL should only
        contain countermeasures declared there.
        ''')
    parser.add_argument(
        "--xbar-only",
        action="store_true",
        help="If defined, the tool generates crossbar RTLs only")
    parser.add_argument(
        "--plic-only",
        action="store_true",
        help="If defined, the tool generates RV_PLIC RTL and Hjson only")
    parser.add_argument(
        "--alert-handler-only",
        action="store_true",
        help="If defined, the tool generates alert handler hjson only")
    parser.add_argument(
        "--rust-only",
        action="store_true",
        help="If defined, the tool generates top Rust code only")
    # Generator options: generate dv ral model
    parser.add_argument(
        "--top_ral",
        "-r",
        default=False,
        action="store_true",
        help="If set, the tool generates top level RAL model for DV")
    parser.add_argument("--alias-files",
                        nargs="+",
                        type=Path,
                        default=None,
                        help="""
          If defined, topgen uses supplied alias hjson file(s) to override the
          generic register definitions when building the RAL model. This
          argument is only relevant in conjunction with the `--top_ral` switch.
        """)
    parser.add_argument(
        "--dv-base-names",
        nargs="+",
        help="Names or prefix for the DV register classes from which "
        "the register models are derived.")
    # Generator options for compile time random netlist constants
    parser.add_argument(
        "--rnd_cnst_seed",
        type=int,
        metavar="<seed>",
        help="Custom seed for RNG to compute netlist constants.")
    # Miscellaneous: only return the list of blocks and exit.
    parser.add_argument("--get_blocks",
                        default=False,
                        action="store_true",
                        help="Only return the list of blocks and exit.")

    args = parser.parse_args()

    # check combinations
    if args.top_ral:
        args.no_top = True

    if (args.no_top or args.no_xbar or
            args.no_plic) and (args.top_only or args.xbar_only or
                               args.plic_only or args.alert_handler_only):
        log.error(
            "'no' series options cannot be used with 'only' series options")
        raise SystemExit(sys.exc_info()[1])

    if args.no_rust and args.rust_only:
        log.error(
            "'no' series options cannot be used with 'only' series options")
        raise SystemExit(sys.exc_info()[1])

    # Don't print warnings when querying the list of blocks.
    log_level = (log.ERROR if args.get_blocks or args.check_cm else
                 log.DEBUG if args.verbose else None)

    log.basicConfig(format="%(levelname)s: %(message)s", level=log_level)

    if not args.outdir:
        outdir = Path(args.topcfg).parents[1]
        log.info("TOP directory not given. Use %s", (outdir))
    elif not Path(args.outdir).is_dir():
        log.error("'--outdir' should point to writable directory")
        raise SystemExit(sys.exc_info()[1])
    else:
        outdir = Path(args.outdir)

    if args.hjson_path is not None:
        log.info(f"Alternate hjson path is {args.hjson_path}")

    out_path = Path(outdir)
    cfg_path = Path(args.topcfg).parents[1]

    try:
        with open(args.topcfg, "r") as ftop:
            topcfg = hjson.load(ftop,
                                use_decimal=True,
                                object_pairs_hook=OrderedDict)
    except ValueError:
        raise SystemExit(sys.exc_info()[1])

    # Extract version stamp from file
    version_stamp = version_file.VersionInformation(args.version_stamp)

    # Initialize RNG for compile-time netlist constants.
    # If specified, override the seed for random netlist constant computation.
    if args.rnd_cnst_seed:
        log.warning("Commandline override of rnd_cnst_seed with {}.".format(
            args.rnd_cnst_seed))
        topcfg["rnd_cnst_seed"] = args.rnd_cnst_seed
    # Otherwise we make sure a seed exists in the HJSON config file.
    elif "rnd_cnst_seed" not in topcfg:
        log.error('Seed "rnd_cnst_seed" not found in configuration HJSON.')
        exit(1)

    secure_prng.reseed(topcfg["rnd_cnst_seed"])

    # TODO, long term, the levels of dependency should be automatically
    # determined instead of hardcoded.  The following are a few examples:
    # Example 1: pinmux depends on amending all modules before calculating the
    #            correct number of pins.
    #            This would be 1 level of dependency and require 2 passes.
    # Example 2: pinmux depends on amending all modules, and pwrmgr depends on
    #            pinmux generation to know correct number of wakeups.  This
    #            would be 2 levels of dependency and require 3 passes.
    #
    # How does multi-pass work?
    # In example 1, the first pass gathers all modules and merges them.
    # However, the merge process uses a stale pinmux.  The correct pinmux is
    # then generated using the merged configuration.  The second pass now merges
    # all the correct modules (including the generated pinmux) and creates the
    # final merged config.
    #
    # In example 2, the first pass gathers all modules and merges them.
    # However, the merge process uses a stale pinmux and pwrmgr.  The correct
    # pinmux is then generated using the merged configuration.  However, since
    # pwrmgr is dependent on this new pinmux, it is still generated incorrectly.
    # The second pass merge now has an updated pinmux but stale pwrmgr.  The
    # correct pwrmgr can now be generated.  The final pass then merges all the
    # correct modules and creates the final configuration.
    #
    # This fix is related to #2083
    process_dependencies = 1

    # topgen generates IP blocks and associated Hjson configuration in multiple
    # steps. After each step, the IP Hjson configuration is read back and then
    # combined into the toplevel configuration. To generate the chip-level RAL,
    # we need to run the full generation step, but ultimately only care about
    # the toplevel configuration (a single Hjson file). Since we don't have a
    # better way at the moment dump all output into a temporary directory, and
    # delete it after the fact, retaining only the toplevel configuration.
    if args.top_ral:
        out_path_gen = Path(tempfile.mkdtemp())
    else:
        out_path_gen = out_path

    for pass_idx in range(process_dependencies + 1):
        log.debug("Generation pass {}".format(pass_idx))
        if pass_idx < process_dependencies:
            cfg_copy = deepcopy(topcfg)
            _, _, _ = _process_top(cfg_copy, args, cfg_path, out_path_gen,
                                   pass_idx)
        else:
            completecfg, name_to_block, name_to_hjson = _process_top(
                topcfg, args, cfg_path, out_path_gen, pass_idx)

    topname = topcfg["name"]
    top_name = f"top_{topname}"

    # Create the chip-level RAL only
    if args.top_ral:
        # See above: we only need `completecfg` and `name_to_block`, not all
        # the other files (e.g. RTL files) generated through topgen.
        shutil.rmtree(out_path_gen, ignore_errors=True)

        exit_code = generate_top_ral(completecfg, name_to_block,
                                     args.dv_base_names, out_path)
        sys.exit(exit_code)

    if args.get_blocks:
        print("\n".join(name_to_block.keys()))
        sys.exit(0)

    # Generate xbars
    if not args.no_xbar or args.xbar_only:
        generate_xbars(completecfg, out_path)

    # All IPs are generated. Connect phase now
    # Find {memory, module} <-> {xbar} connections first.
    im.autoconnect(completecfg, name_to_block)

    # Generic Inter-module connection
    im.elab_intermodule(completecfg)

    # Generate top.gen.hjson right before rendering
    genhjson_dir = out_path / "data/autogen"
    genhjson_dir.mkdir(parents=True, exist_ok=True)
    genhjson_path = genhjson_dir / f"{top_name}.gen.hjson"

    # Header for HJSON
    gencmd = """//
// util/topgen.py -t hw/{top_name}/data/{top_name}.hjson \\
//                -o hw/{top_name}/ \\
//                --rnd_cnst_seed {seed}
""".format(top_name=top_name, seed=completecfg["rnd_cnst_seed"])

    genhjson_path.write_text(genhdr + gencmd +
                             hjson.dumps(completecfg, for_json=True, default=vars) + '\n')

    # Generate Rust toplevel definitions
    if not args.no_rust:
        generate_rust(topname, completecfg, name_to_block, out_path.resolve(),
                      version_stamp, SRCTREE_TOP, TOPGEN_TEMPLATE_PATH)
        if args.rust_only:
            sys.exit(0)

    # Check countermeasures for all blocks.
    if args.check_cm:
        # Change verbosity to log.INFO to see an okay confirmation message:
        # the log level is set to log.ERROR upon start to avoid the chatter
        # of the regular topgen elaboration.
        log.basicConfig(format="%(levelname)s: %(message)s",
                        level=log.INFO,
                        force=True)

        okay = _check_countermeasures(completecfg, name_to_block,
                                      name_to_hjson)
        sys.exit(0 if okay else 1)

    if not args.no_top or args.top_only:

        def render_template(template_path: str, rendered_path: Path,
                            **other_info):
            template_contents = generate_top(completecfg, name_to_block,
                                             str(template_path), **other_info)

            rendered_path.parent.mkdir(exist_ok=True, parents=True)
            with rendered_path.open(mode="w", encoding="UTF-8") as fout:
                fout.write(template_contents)

        # Header for SV files
        gencmd = warnhdr + """//
// util/topgen.py -t hw/{top_name}/data/{top_name}.hjson \\
//                -o hw/{top_name}/ \\
//                --rnd_cnst_seed \\
//                {seed}
""".format(top_name=top_name, seed=completecfg["rnd_cnst_seed"])

        # Top and chiplevel templates are top-specific
        top_template_path = SRCTREE_TOP / "hw" / top_name / "templates"

        # SystemVerilog Top:
        # "toplevel.sv.tpl" -> "rtl/autogen/{top_name}.sv"
        render_template(top_template_path / "toplevel.sv.tpl",
                        out_path / "rtl" / "autogen" / f"{top_name}.sv",
                        gencmd=gencmd)

        # Multiple chip-levels (ASIC, FPGA, Verilator, etc)
        for target in topcfg["targets"]:
            target_name = target["name"]
            render_template(top_template_path / "chiplevel.sv.tpl",
                            out_path /
                            f"rtl/autogen/chip_{topname}_{target_name}.sv",
                            gencmd=gencmd,
                            target=target)

        # The C / SV file needs some complex information, so we initialize this
        # object to store it.
        c_helper = TopGenCTest(completecfg, name_to_block)

        # "toplevel_pkg.sv.tpl" -> "rtl/autogen/{top_name}_pkg.sv"
        render_template(TOPGEN_TEMPLATE_PATH / "toplevel_pkg.sv.tpl",
                        out_path / "rtl" / "autogen" / f"{top_name}_pkg.sv",
                        helper=c_helper,
                        gencmd=gencmd)

        # compile-time random netlist constants
        render_template(TOPGEN_TEMPLATE_PATH / "toplevel_rnd_cnst_pkg.sv.tpl",
                        out_path / f"rtl/autogen/{top_name}_rnd_cnst_pkg.sv",
                        gencmd=gencmd)

        # Since SW does not use FuseSoC and instead expects those files always
        # to be in hw/top_{topname}/sw/autogen, we currently create these files
        # twice:
        # - Once under out_path/sw/autogen
        # - Once under hw/top_{topname}/sw/autogen
        root_paths = [out_path.resolve(), SRCTREE_TOP]
        out_paths = [
            out_path.resolve(), (SRCTREE_TOP / "hw" / top_name).resolve()
        ]
        for idx, path in enumerate(out_paths):
            # C Header + C File + Clang-format file

            # "clang-format" -> "sw/autogen/.clang-format"
            cformat_tplpath = TOPGEN_TEMPLATE_PATH / "clang-format"
            cformat_dir = path / "sw" / "autogen"
            cformat_dir.mkdir(parents=True, exist_ok=True)
            cformat_path = cformat_dir / ".clang-format"
            cformat_path.write_text(cformat_tplpath.read_text())

            # Save the header macro prefix into `c_helper`
            rel_header_dir = cformat_dir.relative_to(root_paths[idx])
            c_helper.header_macro_prefix = (
                "OPENTITAN_" + str(rel_header_dir).replace("/", "_").upper())

            # "{top_name}.h.tpl" -> "sw/autogen/{top_name}.h"
            cheader_path = cformat_dir / f"{top_name}.h"
            render_template(TOPGEN_TEMPLATE_PATH / "toplevel.h.tpl",
                            cheader_path,
                            helper=c_helper)

            # Save the relative header path into `c_helper`
            rel_header_path = cheader_path.relative_to(root_paths[idx])
            c_helper.header_path = str(rel_header_path)

            # "toplevel.c.tpl" -> "sw/autogen/{top_name}.c"
            render_template(TOPGEN_TEMPLATE_PATH / "toplevel.c.tpl",
                            cformat_dir / f"{top_name}.c",
                            helper=c_helper)

            # "toplevel_memory.ld.tpl" -> "sw/autogen/{top_name}_memory.ld"
            render_template(TOPGEN_TEMPLATE_PATH / "toplevel_memory.ld.tpl",
                            cformat_dir / f"{top_name}_memory.ld",
                            helper=c_helper)

            # "toplevel_memory.h.tpl" -> "sw/autogen/{top_name}_memory.h"
            memory_cheader_path = cformat_dir / f"{top_name}_memory.h"
            render_template(TOPGEN_TEMPLATE_PATH / "toplevel_memory.h.tpl",
                            memory_cheader_path,
                            helper=c_helper)

        # generate chip level xbar and alert_handler TB
        tb_files = [
            "xbar_env_pkg__params.sv", "tb__xbar_connect.sv",
            "tb__alert_handler_connect.sv", "xbar_tgl_excl.cfg",
            "rstmgr_tgl_excl.cfg"
        ]
        for fname in tb_files:
            tpl_fname = "%s.tpl" % (fname)
            xbar_chip_data_path = TOPGEN_TEMPLATE_PATH / tpl_fname
            template_contents = generate_top(completecfg,
                                             name_to_block,
                                             str(xbar_chip_data_path),
                                             gencmd=gencmd)

            rendered_dir = out_path / "dv/autogen"
            rendered_dir.mkdir(parents=True, exist_ok=True)
            rendered_path = rendered_dir / fname

            with rendered_path.open(mode="w", encoding="UTF-8") as fout:
                fout.write(template_contents)

        # generate parameters for chip-level environment package
        tpl_fname = "chip_env_pkg__params.sv.tpl"
        alert_handler_chip_data_path = TOPGEN_TEMPLATE_PATH / tpl_fname
        template_contents = generate_top(completecfg, name_to_block,
                                         str(alert_handler_chip_data_path))

        rendered_dir = out_path / "dv/env/autogen"
        rendered_dir.mkdir(parents=True, exist_ok=True)
        rendered_path = rendered_dir / "chip_env_pkg__params.sv"

        with rendered_path.open(mode="w", encoding="UTF-8") as fout:
            fout.write(template_contents)

        # generate documentation for toplevel
        gen_top_docs(completecfg, c_helper, out_path)

        # Auto-generate tests in "sw/device/tests/autogen" area.
        gencmd = warnhdr + GENCMD.format(top_name=top_name)
        for fname in ["plic_all_irqs_test.c", "alert_test.c", "BUILD"]:
            outfile = SRCTREE_TOP / "sw/device/tests/autogen" / fname
            render_template(TOPGEN_TEMPLATE_PATH / f"{fname}.tpl",
                            outfile,
                            helper=c_helper,
                            gencmd=gencmd)


if __name__ == "__main__":
    main()
