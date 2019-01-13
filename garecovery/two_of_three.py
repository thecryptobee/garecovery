import decimal
import io
import logging
import math
import os
import shutil
import time

from gaservices.utils import gacommon, gaconstants, txutil

import wallycore as wally

from . import bitcoincore
from . import clargs
from . import exceptions
from . import ga_xpub
from . import util


def get_scriptpubkey_hex(redeem_script_hash_hex):
    """Return a 2of3 multisig script pub key as a hex string"""
    return "a914{}87".format(redeem_script_hash_hex)


def get_redeem_script(keys):
    """Return a 2of3 multisig redeem script as a hex string"""
    keys = [wally.hex_from_bytes(key) for key in keys]
    print('-------------------------------------------------------------------')
    print("get_redeem_script public keys = {}".format(keys))
    return wally.hex_to_bytes("5221{}21{}21{}53ae".format(*keys))


def bip32_key_from_base58check(base58check):
    raw = wally.base58check_to_bytes(base58check)
    return wally.bip32_key_unserialize(raw)


def derive_user_key(wallet, subaccount, branch=1):
    print('-------------------------------------------------------------------')
    print('@derive_user_key, subaccount:', subaccount)
    subaccount_path = gacommon.get_subaccount_path(subaccount)
    print('-------------------------------------------------------------------')
    print('@derive_user_key, path:', subaccount_path)
    return gacommon.derive_hd_key(wallet, subaccount_path + [branch])


class P2SH:

    type_ = 'p2sh'

    def __init__(self, pubkeys, network):
        self.redeem_script = get_redeem_script(pubkeys)
        self.redeem_script_hex = wally.hex_from_bytes(self.redeem_script)

        script_hash = wally.hash160(self.get_witness_script())
        script_hash_hex = wally.hex_from_bytes(script_hash)
        self.scriptPubKey = get_scriptpubkey_hex(script_hash_hex)

        ver = {'testnet': b'\xc4', 'mainnet': b'\x05'}[network]
        self.address = wally.base58check_from_bytes(ver + script_hash)

    def get_witness_script(self):
        return self.redeem_script


class P2WSH(P2SH):

    type_ = 'p2wsh'

    def __init__(self, pubkeys, network):
        P2SH.__init__(self, pubkeys, network)

    def get_witness_script(self):
        return wally.witness_program_from_bytes(self.redeem_script, wally.WALLY_SCRIPT_SHA256)


def createDerivedKeySet(ga_xpub, wallets, custom_xprv, network):
    """Return class instances which represent sets of derived keys

    Given a user's key material call createDerivedKeySet to create a class
    instances of which can be created by specifying the pointer value. Each
    such instance then represents a set of keys derived on the path specified
    by that pointer.
    """
    # The GreenAddress extended public key (ga_xpub) also contains the subaccount index encoded
    # as the child_num
    subaccount = wally.bip32_key_get_child_num(ga_xpub)
    print('----------------------------------------------------------------------------')
    print('subaccount derivation from ga_xpub:', subaccount)

    # Given the subaccount the user keys can be derived. Optionally the user may provide a custom
    # extended private key as the backup
    user_keys = [derive_user_key(wallet, subaccount) for wallet in wallets]
    print('-------------------------------------------------------------------')
    print("User xpubs: ", user_keys[0], str(user_keys[0]))
    if custom_xprv:
        print("Using custom xprv")
        root_xprv = bip32_key_from_base58check(custom_xprv)
        branch = 1
        xprv = gacommon.derive_hd_key(root_xprv, [branch], wally.BIP32_FLAG_KEY_PRIVATE)
        user_keys.append(xprv)
    assert len(user_keys) == 2

    class DerivedKeySet:
        """Represent sets of HD keys
        """

        def __init__(self, pointer):
            print('-------------------------------------------------------------------')
            print('Derive keys for subaccount={}, pointer={}'.format(subaccount, pointer))

            self.subaccount = subaccount
            self.pointer = pointer

            # Derive the GreenAddress public key for this pointer value
            ga_key = gacommon.derive_hd_key(ga_xpub, [pointer], wally.BIP32_FLAG_KEY_PUBLIC)
            self.ga_key = wally.bip32_key_get_pub_key(ga_key)
            print("ga_key = {}".format(wally.hex_from_bytes(self.ga_key)))

            # Derive the user private keys for this pointer value
            flags = wally.BIP32_FLAG_KEY_PRIVATE
            user_key_paths = [(key, [pointer]) for key in user_keys]
            private_keys = [gacommon.derive_hd_key(*path, flags=flags) for path in user_key_paths]
            self.private_keys = [wally.bip32_key_get_priv_key(k) for k in private_keys]
            print('-------------------------------------------------------------------')
            privateKeysHex = [wally.hex_from_bytes(key) for key in self.private_keys]
            print('privateKeys:', privateKeysHex)

            # Derive the user public keys from the private keys
            user_public_keys = [wally.ec_public_key_from_private_key(k) for k in self.private_keys]
            public_keys = [self.ga_key] + user_public_keys
            print('-------------------------------------------------------------------')
            publicKeysHex = [wally.hex_from_bytes(key) for key in public_keys]
            print('publicKeys:', publicKeysHex)

            # Script could be segwit or not - generate both segwit and non-segwit addresses
            self.witnesses = {cls.type_: cls(public_keys, network) for cls in (P2SH, P2WSH)}
            print('-------------------------------------------------------------------')
            print('p2sh address: {}'.format(self.witnesses['p2sh'].address))
            print('p2wsh address: {}'.format(self.witnesses['p2wsh'].address))

    return DerivedKeySet


class UTXO:

    def __init__(self, keyset, witness_type, vout, tx, dest_address):
        assert vout < wally.tx_get_num_outputs(tx)

        self.keyset = keyset
        self.vout = vout
        self.tx = tx
        self.txhash_bin = txutil.get_txhash_bin(tx)
        self.dest_address = dest_address
        self.witness = self.keyset.witnesses[witness_type]

    def get_default_feerate(self):
        """Get a value for default feerate.

        On testnet only it is possible to pass --default-feerate as an option. On mainnet this is
        not supported as it is too error prone.
        """
        if clargs.args.default_feerate is None:
            msg = 'Unable to get fee rate from core, you must pass --default-feerate'
            raise exceptions.NoFeeRate(msg)

        fee_satoshi_byte = decimal.Decimal(clargs.args.default_feerate)
        return fee_satoshi_byte

    def get_feerate(self):
        """Return the required fee rate in satoshis per byte"""
        print("Connecting to bitcoinrpc to get feerate")
        core = bitcoincore.Connection(clargs.args)

        blocks = clargs.args.fee_estimate_blocks

        fee_btc_kb = core.estimatesmartfee(blocks)['feerate']
        if fee_btc_kb == -1:
            # estimatesmartfee returns -1 to indicate it is unable to provide a fee estimate
            fee_satoshi_byte = self.get_default_feerate()
        else:
            fee_satoshi_kb = fee_btc_kb * gaconstants.SATOSHI_PER_BTC
            fee_satoshi_byte = round(fee_satoshi_kb / 1000)

            print('feerate = {} BTC/kb'.format(fee_btc_kb))
            print('feerate = {} satoshis/kb'.format(fee_satoshi_kb))

        print('Fee estimate for confirmation in {} blocks is '
                     '{} satoshis/byte'.format(blocks, fee_satoshi_byte))

        return fee_satoshi_byte

    def get_raw_unsigned(self, fee_satoshi):
        """Return raw transaction ready for signing

        May return a transaction with amount=0 if the input amount is not enough to cover fees
        """
        amount_satoshi = wally.tx_get_output_satoshi(self.tx, self.vout)

        if fee_satoshi >= amount_satoshi:
            print('Insufficient funds to cover fee')
            print('txout has value of {}, fee = {}'.format(amount_satoshi, fee_satoshi))

        # Calculate adjusted amount = input amount - fee
        adjusted_amount_satoshi = max(0, amount_satoshi - fee_satoshi)
        print('tx amount = amount - fee = {} - {} = {}'.format(
            amount_satoshi, fee_satoshi, adjusted_amount_satoshi))
        assert adjusted_amount_satoshi >= 0

        print("Create tx: {} sat -> {}".format(adjusted_amount_satoshi, self.dest_address))

        # Set nlocktime to the current blockheight to discourage 'fee sniping', as per the core
        # wallet implementation
        tx = txutil.new(util.get_current_blockcount() or 0, version=1)
        seq = gaconstants.MAX_BIP125_RBF_SEQUENCE
        txutil.add_input(tx, self.txhash_bin, self.vout, seq)
        scriptpubkey = util.scriptpubkey_from_address(self.dest_address)
        txutil.add_output(tx, adjusted_amount_satoshi, scriptpubkey)
        return txutil.to_hex(tx)

    def get_fee(self, tx):
        """Given a raw transaction return the fee"""
        virtual_tx_size = wally.tx_get_vsize(tx)
        print("virtual transaction size = {}".format(virtual_tx_size))
        fee_satoshi_byte = self.get_feerate()
        fee_satoshi = fee_satoshi_byte * virtual_tx_size
        print('Calculating fee over {} (virtual) byte tx @{} satoshi per '
                      'byte = {} satoshis'.format(virtual_tx_size, fee_satoshi_byte, fee_satoshi))
        return fee_satoshi

    def sign(self):
        """Return raw signed transaction"""
        type_map = {'p2wsh': gaconstants.P2SH_P2WSH_FORTIFIED_OUT, 'p2sh': 0}
        txdata = {
            'prevout_scripts': [self.witness.redeem_script_hex],
            'prevout_script_types': [type_map[self.witness.type_]],
            'prevout_values': [wally.tx_get_output_satoshi(self.tx, self.vout)]
        }
        signatories = [gacommon.ActiveSignatory(key) for key in self.keyset.private_keys]

        def sign_(fee):
            txdata['tx'] = self.get_raw_unsigned(fee)
            return gacommon.sign(
                txdata,
                signatories,
            )

        signed_no_fee = sign_(fee=1000)
        fee_satoshi = self.get_fee(signed_no_fee)
        signed_fee = sign_(fee=fee_satoshi)
        return txutil.to_hex(signed_fee)


class TwoOfThree(object):

    def __init__(self, mnemonic, wallet, backup_wallet, custom_xprv):
        self.mnemonic = mnemonic
        self.wallets = [wallet]
        self.custom_xprv = custom_xprv

        if backup_wallet:
            self.wallets.append(backup_wallet)
        else:
            assert self.custom_xprv
            print('Using custom xprv = {}'.format(self.custom_xprv))

        inferred_network = self.infer_network()
        if inferred_network != clargs.args.network:
            msg = 'Specified network and network inferred from address do not match' \
                  '(specified={}, inferred={})'.format(clargs.args.network, inferred_network)
            raise exceptions.InvalidNetwork(msg)

        if clargs.args.network != 'testnet' and clargs.args.default_feerate:
            # For non-testnet addresses do not support --default-feerate
            msg = '--default-feerate can be used only in testnet'
            raise exceptions.NoFeeRate(msg)

    def get_destination_address(self):
        """Return the destination address to recover funds to"""
        return clargs.args.destination_address

    def infer_network(self):
        """Infer network from the destination address"""
        return util.network_from_address(self.get_destination_address())

    def scan_blockchain(self, keysets):
        # Blockchain scanning is delegated to core via bitcoinrpc
        print("Connecting to bitcoinrpc to scan blockchain")
        core = bitcoincore.Connection(clargs.args)

        if core.getnetworkinfo()["version"] == 170000 and clargs.args.ignore_mempool:
            print('Mempool transactions are being ignored')
            # If the node is running version 0.17.0 and
            # the user does not want to scan the mempool, then use
            # scantxoutset, otherwise fall back to importmulti + listunspent
            # FIXME: check for format changes in 0.17.1

            scanobjects = []
            for keyset in keysets:
                for witness in keyset.witnesses.values():
                    scanobjects.append('addr({})'.format(witness.address))
                    # By using the descriptor "addr(<address>)" we do not fully exploit
                    # the potential of output descriptors (we could delegate the HD
                    # derivation to core). However, as long as the RPC will be marked as
                    # experimental, it is better to keep its usage simple.
            print('Scanning UTXO set for {} derived addresses'.format(len(scanobjects)))
            all_utxos = core.scantxoutset("start", scanobjects)["unspents"]
            print('Unspents: {}'.format(all_utxos))
        elif not clargs.args.ignore_mempool:
            print("Scanning from '{}'".format(clargs.args.scan_from))
            print('This step may take 10 minutes or more')

            # Need to import our keysets into core so that it will recognise the
            # utxos we are looking for
            addresses = []
            requests = []
            for keyset in keysets:
                for witness in keyset.witnesses.values():
                    addresses.append(witness.address)
                    requests.append({
                        'scriptPubKey': {"address": witness.address},
                        'timestamp': clargs.args.scan_from,
                        'watchonly': True,
                    })
            print('Importing {} derived addresses into bitcoind'.format(len(requests)))
            result = core.importmulti(requests)
            expected_result = [{'success': True}] * len(requests)
            if result != expected_result:
                print('Unexpected result from importmulti')
                print('Expected: {}'.format(expected_result))
                print('Actual: {}'.format(result))
                raise exceptions.ImportMultiError('Unexpected result from importmulti')
            print('Successfully imported {} derived addresses'.format(len(result)))

            # Scan the blockchain for any utxos with addresses that match the derived keysets
            print('Getting unspent transactions...')
            all_utxos = core.listunspent(0, 9999999, addresses)
            print('all utxos = {}'.format(all_utxos))
            print('There are {} unspent transactions'.format(len(all_utxos)))
        else:
            # The flag --ingore-mempool is not intended to ignore the mempool, but just to
            # make the user aware that `scantxoutset` does not look at mempool transactions.
            msg = '--ignore-mempool cannot be specified if you run an old version of ' \
                  'Bitcoin Core (without scantxoutset)'
            raise exceptions.BitcoinCoreConnectionError(msg)

        # Now need to match the returned utxos with the keysets that unlock them
        # This is a rather unfortunate loop because there is no other way to correlate the
        # results from listunspent with the requests to importmulti, or infer the order
        # of the outputs from scantxoutset
        utxos = []
        tx_matches = [(tx['txid'], keyset, witness, tx['vout'])
                      for tx in all_utxos
                      for keyset in keysets
                      for witness in keyset.witnesses.values()
                      if tx['scriptPubKey'] == witness.scriptPubKey]

        raw_txs = core.batch_([["getrawtransaction", tx[0]] for tx in tx_matches])
        dest_address = self.get_destination_address()
        for txid_match, raw_tx in zip(tx_matches, raw_txs):
            txid, keyset, witness, txvout = txid_match
            print('Found recoverable transaction, '
                         'subaccount={}, pointer={}, txid={}, witness type={}'.
                         format(keyset.subaccount, keyset.pointer, txid,
                                witness.type_))
            print("found raw={}".format(raw_tx))
            utxo = UTXO(
                keyset,
                witness.type_,
                txvout,
                txutil.from_hex(raw_tx),
                dest_address,
            )
            utxos.append(utxo)
        return utxos

    def _derived_keyset(self, ga_xpub):
        """Call createDerivedKeySet with ga_xpub"""
        return createDerivedKeySet(ga_xpub, self.wallets, self.custom_xprv, clargs.args.network)

    def get_keysets(self, subaccounts, pointers):
        """Return the keysets for a set of subaccounts/pointers"""
        # There are two options here:
        # 1) If the GreenAddress extended public key (ga_xpub) has been specified then not only
        #    does it not need to be derived but it also contains within its serialization format
        #    the subaccount index (in the child_num field)
        # 2) If ga_xpub is not given it's possible to iterate over the possible values of
        #    subaccount and build a larger search space. This is suboptimal
        if clargs.args.search_subaccounts:
            print('No --ga-xpub specified, deriving and iterating over possible subaccounts')
            keyset_factories = []
            for subaccount in range(*subaccounts):
                xpubs = ga_xpub.xpubs_from_mnemonic(self.mnemonic, subaccount, clargs.args.network)
                keyset_factories.extend([self._derived_keyset(xpub) for xpub in xpubs])
        else:
            assert clargs.args.ga_xpub
            xpub = bip32_key_from_base58check(clargs.args.ga_xpub)
            keyset_factories = [self._derived_keyset(xpub)]

        return [DerivedKeySet(pointer)
                for DerivedKeySet in keyset_factories
                for pointer in range(*pointers)]

    def get_utxos(self, subaccounts, pointers):
        keysets = self.get_keysets(subaccounts, pointers)
        # return self.scan_blockchain(keysets)
        return False

    def rescan(self, pointer_search_depth, num_subaccounts):
        pointers = (0, pointer_search_depth)
        subaccounts = (1, 1 + num_subaccounts)

        utxos = []
        while True:
            print("Scanning subaccount {}->{}, pointers {}->{}".format(
                subaccounts[0], subaccounts[1], pointers[0], pointers[1]))

            next_utxos = self.get_utxos(subaccounts, pointers)
            if not next_utxos:
                print('No transactions found, stopping scan')
                break

            # As long as some utxos have been found in the range (pointers), keep scanning
            print('Found {} transactions'.format(len(next_utxos)))
            utxos.extend(next_utxos)
            pointers = (pointers[1], pointers[1] + pointer_search_depth)

        return utxos

    def sign_utxos(self):
        print("signing {} utxos...".format(len(self.utxos)))
        return [utxo.sign() for utxo in self.utxos]

    def get_transactions(self):
        # Get a list of utxos by scanning the blockchain
        self.utxos = self.rescan(
            clargs.args.key_search_depth,
            clargs.args.search_subaccounts or 0)

        return [(txutil.from_hex(tx), None) for tx in self.sign_utxos()]
