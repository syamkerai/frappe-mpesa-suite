# Copyright (c) 2026, Astral Industries Limited
# For license information, please see license.txt
"""Fill M-Pesa Known Name (round 4.5) from the payouts made before it existed.

Every Paid B2C Disbursement Request and Mpesa B2B Payment Request that names whom M-Pesa paid gives
its phone, till or paybill a record: the latest name, first and last seen, how many payouts, the last
payout (source History), salary-only where every payout was a salary, the bank flag for a bank's
paybill. From then on each new Result adds to it (money_out.known_names). Idempotent: running it
again writes the same values. At Astral: about 2,700 payouts naming some 440 numbers and tills, a
few seconds.
"""

from mpsa_suite.handover import ran_under_old_name


def execute():
    if ran_under_old_name(__name__):
        return  # R8: the patch convention; never registered under the old name
    from mpsa_suite.money_out import known_names

    known_names.backfill()
