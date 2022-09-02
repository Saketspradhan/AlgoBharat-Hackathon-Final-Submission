from pyteal import *
from pyteal.ast import *
from pyteal.ast.bytes import Bytes
from pyteal_helpers import program

from base64 import b64decode
from dataclasses import dataclass
from typing import Dict

from algosdk.v2client.algod import AlgodClient
from algosdk import account
from algosdk.future import transaction
from algosdk.kmd import KMDClient
from algosdk.v2client.algod import AlgodClient


class AppVariables:
    asaSellerAddress = "asaSellerAddress"
    highestBid = "HighestBid"
    asaOwnerAddress = "ASAOwnerAddress"
    asaDelegateAddress = "ASADelegateAddress"
    algoDelegateAddress = "AlgoDelegateAddress"
    appStartRound = "appStartRound"
    appEndRound = "appEndRound"


    def application_start(initialization_code,
                        application_actions):
        is_app_initialization = Txn.application_id() == Int(0)
        are_actions_used = Txn.on_completion() == OnComplete.NoOp

        return If(is_app_initialization, initialization_code,
                If(are_actions_used, application_actions, Return(Int(0))))


    def app_initialization_logic():
        return Seq([
            App.globalPut(Bytes(AppVariables.highestBid), Int(DefaultValues.highestBid)),
            App.globalPut(Bytes(AppVariables.appStartRound), Global.round()),
            Return(Int(1))
        ])


    def setup_possible_app_calls_logic(asset_authorities_code, transfer_asa_code, payment_to_seller_code):
        is_setting_up_asset_authorities = Global.group_size() == Int(1)
        is_transferring_asa = Global.group_size() == Int(4)
        is_payment_to_seller = Global.group_size() == Int(2)

        return If(is_setting_up_asset_authorities, asset_authorities_code,
                If(is_transferring_asa, transfer_asa_code,
                    If(is_payment_to_seller, payment_to_seller_code, Return(Int(0)))))


    def setup_asset_delegates_logic():
        asa_delegate_authority = App.globalGetEx(Int(0), Bytes(AppVariables.asaDelegateAddress))
        algo_delegate_authority = App.globalGetEx(Int(0), Bytes(AppVariables.algoDelegateAddress))

        setup_failed = Seq([
            Return(Int(0))
        ])

        start_round = App.globalGet(Bytes(AppVariables.appStartRound))

        setup_authorities = Seq([
            App.globalPut(Bytes(AppVariables.asaDelegateAddress), Txn.application_args[0]),
            App.globalPut(Bytes(AppVariables.algoDelegateAddress), Txn.application_args[1]),
            App.globalPut(Bytes(AppVariables.asaOwnerAddress), Txn.application_args[2]),
            App.globalPut(Bytes(AppVariables.appEndRound), Add(start_round, Btoi(Txn.application_args[3]))),
            App.globalPut(Bytes(AppVariables.asaSellerAddress), Txn.application_args[4]),
            Return(Int(1))
        ])

    def asa_transfer_logic():
        # Valid first transaction
        valid_first_transaction = Gtxn[0].type_enum() == TxnType.ApplicationCall

        # Valid second transaction
        second_transaction_is_payment = Gtxn[1].type_enum() == TxnType.Payment
        do_first_two_transaction_have_same_sender = Gtxn[1].sender() == Gtxn[0].sender()

        current_highest_bid = App.globalGet(Bytes(AppVariables.highestBid))
        is_valid_amount_to_change_titles = Gtxn[1].amount() > current_highest_bid

        algo_delegate_address = App.globalGet(Bytes(AppVariables.algoDelegateAddress))
        is_paid_to_algo_delegate_address = Gtxn[1].receiver() == algo_delegate_address

        valid_second_transaction = And(second_transaction_is_payment,
                                    do_first_two_transaction_have_same_sender,
                                    is_valid_amount_to_change_titles,
                                    is_paid_to_algo_delegate_address)

        # Valid third transaction
        old_owner_address = App.globalGet(Bytes(AppVariables.asaOwnerAddress))

        third_transaction_is_payment = Gtxn[2].type_enum() == TxnType.Payment
        is_paid_from_algo_delegate_authority = Gtxn[2].sender() == algo_delegate_address
        is_paid_to_old_owner = Gtxn[2].receiver() == old_owner_address
        is_paid_right_amount = Gtxn[2].amount() == current_highest_bid

        valid_third_transaction = And(third_transaction_is_payment,
                                    is_paid_from_algo_delegate_authority,
                                    is_paid_to_old_owner,
                                    is_paid_right_amount)

        # Valid fourth transaction
        asa_delegate_address = App.globalGet(Bytes(AppVariables.asaDelegateAddress))

        fourth_transaction_is_asset_transfer = Gtxn[3].type_enum() == TxnType.AssetTransfer
        is_paid_from_asa_delegate_authority = Gtxn[3].sender() == asa_delegate_address
        is_the_new_owner_receiving_the_asa = Gtxn[3].asset_receiver() == Gtxn[1].sender()

        valid_forth_transaction = And(fourth_transaction_is_asset_transfer,
                                    is_paid_from_asa_delegate_authority,
                                    is_the_new_owner_receiving_the_asa)

        # Valid time
        end_round = App.globalGet(Bytes(AppVariables.appEndRound))
        is_app_active = Global.round() <= end_round

        # Updating the app state
        update_highest_bid = App.globalPut(Bytes(AppVariables.highestBid), Gtxn[1].amount())
        update_owner_address = App.globalPut(Bytes(AppVariables.asaOwnerAddress), Gtxn[1].sender())
        update_app_state = Seq([
            update_highest_bid,
            update_owner_address,
            Return(Int(1))
        ])

        are_valid_transactions = And(valid_first_transaction,
                                    valid_second_transaction,
                                    valid_third_transaction,
                                    valid_forth_transaction,
                                    is_app_active)

        return If(are_valid_transactions, update_app_state, Seq([Return(Int(0))]))


    def approval_program():
        return application_start(initialization_code=app_initialization_logic(),
                                application_actions=
                                setup_possible_app_calls_logic(asset_authorities_code=setup_asset_authorities_logic(),
                                                                transfer_asa_code=asa_transfer_logic(),
                                                                payment_to_seller_code=payment_to_seller_logic()))


    def clear_program():
        return Return(Int(1))

######################################################################################################################

class AppInitializationService:
    def __init__(self,
                app_creator_pk: str,
                app_creator_address: str,
                asa_unit_name: str,
                asa_asset_name: str,
                app_duration: int,
                teal_version: int = 3):
        self.app_creator_pk = app_creator_pk
        self.app_creator_address = app_creator_address
        self.asa_unit_name = asa_unit_name
        self.asa_asset_name = asa_asset_name
        self.app_duration = app_duration
        self.teal_version = teal_version

        self.client = developer_credentials.get_client()
        self.approval_program_code = approval_program()
        self.clear_program_code = clear_program()

        self.app_id = -1
        self.asa_id = -1
        self.asa_delegate_authority_address = ''
        self.algo_delegate_authority_address = ''


    def create_application(self):
        approval_program_compiled = compileTeal(self.approval_program_code,
                                                mode=Mode.Application,
                                                version=self.teal_version)
        clear_program_compiled = compileTeal(self.clear_program_code,
                                            mode=Mode.Application,
                                            version=self.teal_version)

        approval_program_bytes = blockchain_utils.compile_program(client=self.client,
                                                                source_code=approval_program_compiled)

        clear_program_bytes = blockchain_utils.compile_program(client=self.client,
                                                            source_code=clear_program_compiled)

        global_schema = algo_txn.StateSchema(num_uints=AppVariables.number_of_int(),
                                            num_byte_slices=AppVariables.number_of_str())

        local_schema = algo_txn.StateSchema(num_uints=0,
                                            num_byte_slices=0)

        self.app_id = blockchain_utils.create_application(client=self.client,
                                                        creator_private_key=self.app_creator_pk,
                                                        approval_program=approval_program_bytes,
                                                        clear_program=clear_program_bytes,
                                                        global_schema=global_schema,
                                                        local_schema=local_schema,
                                                        app_args=None)

########################################################################################################################

class AppInteractionService:
    def __init__(self,
                 app_id: int,
                 asa_id: int,
                 current_owner_address: str,
                 current_highest_bid: int = DefaultValues.highestBid,
                 teal_version: int = 3):
        self.client = developer_credentials.get_client()
        self.app_id = app_id
        self.asa_id = asa_id
        self.current_owner_address = current_owner_address
        self.current_highest_bid = current_highest_bid
        self.teal_version = teal_version

        asa_delegate_authority_compiled = compileTeal(asa_delegate_authority_logic(app_id=self.app_id,
                                                                                   asa_id=self.asa_id),
                                                      mode=Mode.Signature,
                                                      version=self.teal_version)

        self.asa_delegate_authority_code_bytes = blockchain_utils.compile_program(client=self.client,
                                             source_code=asa_delegate_authority_compiled)

        self.asa_delegate_authority_address = algo_logic.address(self.asa_delegate_authority_code_bytes)

        algo_delegate_authority_compiled = compileTeal(algo_delegate_authority_logic(app_id=self.app_id),
                                                       mode=Mode.Signature,
                                                       version=self.teal_version)

        self.algo_delegate_authority_code_bytes = blockchain_utils.compile_program(client=self.client,
                                             source_code=algo_delegate_authority_compiled)

        self.algo_delegate_authority_address = algo_logic.address(self.algo_delegate_authority_code_bytes)                                            


    def execute_bidding(self,
                            bidder_name: str,
                            bidder_private_key: str,
                            bidder_address: str,
                            amount: int):
            params = blockchain_utils.get_default_suggested_params(client=self.client)

            # 1. Application call txn
            bidding_app_call_txn = algo_txn.ApplicationCallTxn(sender=bidder_address,
                                                            sp=params,
                                                            index=self.app_id,
                                                            on_complete=algo_txn.OnComplete.NoOpOC)

            # 2. Bidding payment transaction
            biding_payment_txn = algo_txn.PaymentTxn(sender=bidder_address,
                                                    sp=params,
                                                    receiver=self.algo_delegate_authority_address,
                                                    amt=amount)

            # 3. Payment txn from algo delegate authority the current owner
            algo_refund_txn = algo_txn.PaymentTxn(sender=self.algo_delegate_authority_address,
                                                sp=params,
                                                receiver=self.current_owner_address,
                                                amt=self.current_highest_bid)

            # 4. Asa opt-in for the bidder & asset transfer transaction
            blockchain_utils.asa_opt_in(client=self.client,
                                        sender_private_key=bidder_private_key,
                                        asa_id=self.asa_id)

            asa_transfer_txn = algo_txn.AssetTransferTxn(sender=self.asa_delegate_authority_address,
                                                        sp=params,
                                                        receiver=bidder_address,
                                                        amt=1,
                                                        index=self.asa_id,
                                                        revocation_target=self.current_owner_address)

            # Atomic transfer
            gid = algo_txn.calculate_group_id([bidding_app_call_txn,
                                            biding_payment_txn,
                                            algo_refund_txn,
                                            asa_transfer_txn])

            bidding_app_call_txn.group = gid
            biding_payment_txn.group = gid
            algo_refund_txn.group = gid
            asa_transfer_txn.group = gid

            bidding_app_call_txn_signed = bidding_app_call_txn.sign(bidder_private_key)
            biding_payment_txn_signed = biding_payment_txn.sign(bidder_private_key)

            algo_refund_txn_logic_signature = algo_txn.LogicSig(self.algo_delegate_authority_code_bytes)
            algo_refund_txn_signed = algo_txn.LogicSigTransaction(algo_refund_txn, algo_refund_txn_logic_signature)

            asa_transfer_txn_logic_signature = algo_txn.LogicSig(self.asa_delegate_authority_code_bytes)
            asa_transfer_txn_signed = algo_txn.LogicSigTransaction(asa_transfer_txn, asa_transfer_txn_logic_signature)

            signed_group = [bidding_app_call_txn_signed,
                            biding_payment_txn_signed,
                            algo_refund_txn_signed,
                            asa_transfer_txn_signed]

            txid = self.client.send_transactions(signed_group)

            blockchain_utils.wait_for_confirmation(self.client, txid)

            self.current_owner_address = bidder_address
            self.current_highest_bid = amount


    def pay_to_seller(self, asa_seller_address):

            params = blockchain_utils.get_default_suggested_params(client=self.client)

            # 1. Application call txn
            bidding_app_call_txn = algo_txn.ApplicationCallTxn(sender=self.algo_delegate_authority_address,
                                                            sp=params,
                                                            index=self.app_id,
                                                            on_complete=algo_txn.OnComplete.NoOpOC)

            # 2. Payment transaction
            algo_refund_txn = algo_txn.PaymentTxn(sender=self.algo_delegate_authority_address,
                                                sp=params,
                                                receiver=asa_seller_address,
                                                amt=self.current_highest_bid)

            # Atomic transfer
            gid = algo_txn.calculate_group_id([bidding_app_call_txn,
                                            algo_refund_txn])

            bidding_app_call_txn.group = gid
            algo_refund_txn.group = gid

            bidding_app_call_txn_logic_signature = algo_txn.LogicSig(self.algo_delegate_authority_code_bytes)
            bidding_app_call_txn_signed = algo_txn.LogicSigTransaction(bidding_app_call_txn,
                                                                    bidding_app_call_txn_logic_signature)

            algo_refund_txn_logic_signature = algo_txn.LogicSig(self.algo_delegate_authority_code_bytes)
            algo_refund_txn_signed = algo_txn.LogicSigTransaction(algo_refund_txn, algo_refund_txn_logic_signature)

            signed_group = [bidding_app_call_txn_signed,
                            algo_refund_txn_signed]

            txid = self.client.send_transactions(signed_group)

            blockchain_utils.wait_for_confirmation(self.client, txid)

#############################################################################################################################

app_initialization_service = AppInitializationService(app_creator_pk=main_dev_pk,
                                                      app_creator_address=main_dev_address,
                                                      asa_unit_name="wawa",
                                                      asa_asset_name="wawa",
                                                      app_duration=150,
                                                      teal_version=3)

app_initialization_service.create_application()

app_initialization_service.create_asa()

app_initialization_service.setup_asa_delegate_smart_contract()
app_initialization_service.deposit_fee_funds_to_asa_delegate_authority()

app_initialization_service.change_asa_credentials()

app_initialization_service.setup_algo_delegate_smart_contract()
app_initialization_service.deposit_fee_funds_to_algo_delegate_authority()

app_initialization_service.setup_app_delegates_authorities()
 