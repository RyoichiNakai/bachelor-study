import time
import socket, threading, json
import copy
import pickle
import os

from blockchain.blockchain_manager import BlockchainManager
from blockchain.block_builder import BlockBuilder
from transaction.transaction_pool import TransactionPool
from transaction.verify_locinfo import VerifyLocinfo
from p2p.connection_manager import ConnectionManager
from p2p.my_protocol_message_handler import MyProtocolMessageHandler
from p2p.message_manager import (
    MSG_NEW_TRANSACTION,
    MSG_NEW_BLOCK,
    MSG_REQUEST_FULL_CHAIN,
    RSP_FULL_CHAIN,
    MSG_ENHANCED,
)

STATE_INIT = 0
STATE_STANDBY = 1
STATE_CONNECTED_TO_NETWORK = 2
STATE_SHUTTING_DOWN = 3

# TransactionPoolの確認頻度
# 動作チェック用に数字小さくしてるけど、600(10分)くらいはあって良さそ
# ここのインターバルでブロックチェーンの作る速度を決めることができう
CHECK_INTERVAL = 10


class ServerCore(object):

    def __init__(self, my_port = 50082, core_node_host=None, core_node_port=None):
        self.server_state = STATE_INIT
        print('Initializing server...')
        self.my_ip = self.__get_myip()
        print('Server IP address is set to ... ', self.my_ip)
        self.my_port = my_port
        self.cm = ConnectionManager(self.my_ip, self.my_port, self.__handle_message)
        self.mpmh = MyProtocolMessageHandler()
        self.core_node_host = core_node_host
        self.core_node_port = core_node_port
        self.bb = BlockBuilder()
        self.flag_stop_block_build = False
        self.is_bb_running = False
        my_genesis_block = self.bb.generate_genesis_block()
        self.bm = BlockchainManager(my_genesis_block.to_dict())
        self.prev_block_hash = self.bm.get_hash(my_genesis_block.to_dict())    
        self.tp = TransactionPool()
        self.vl = VerifyLocinfo()
        # 位置情報の偽装を検知するようのカウンタ
        self.c_cnt = 0
        self.bc_cnt = 0
        self.false_cnt = 0


    def start_block_building(self):
        self.bb_timer = threading.Timer(CHECK_INTERVAL, self.__generate_block_with_tp)
        self.bb_timer.start()

    def stop_block_building(self):
        print('Thread for __generate_block_with_tp is stopped now')
        self.bb_timer.cancel()

    def start(self):
        self.server_state = STATE_STANDBY
        self.cm.start()
        self.start_block_building()

    def join_network(self):
        if self.core_node_host != None:
            self.server_state = STATE_CONNECTED_TO_NETWORK # 状態：親ノードへ接続中
            self.cm.join_network(self.core_node_host, self.core_node_port)
        else:
            print('This server is running as Genesis Core Node...')

    def shutdown(self):
        self.server_state = STATE_SHUTTING_DOWN # 状態：切断中
        print('Shutdown server...')
        self.cm.connection_close()
        self.stop_block_building()

    def get_my_current_state(self):
        return self.server_state

    def get_all_chains_for_resolve_conflict(self):
        print('get_all_chains_for_resolve_conflict called')
        new_message = self.cm.get_message_text(MSG_REQUEST_FULL_CHAIN)
        self.cm.send_msg_to_all_peer(new_message)

    def __generate_block_with_tp(self):

        print('Thread for generate_block_with_tp started!')
        while self.flag_stop_block_build is not True:

            result = self.tp.get_stored_transactions()

            if result == None:
                print('Transaction Pool is empty ...')
                break

            new_tp = self.bm.remove_useless_transaction(result)
            self.tp.renew_my_transactions(new_tp)
            if len(new_tp) == 0:
                break
            new_block = self.bb.generate_new_block(new_tp, self.prev_block_hash)
            self.bm.set_new_block(new_block.to_dict())
            self.prev_block_hash = self.bm.get_hash(new_block.to_dict())
            message_new_block = self.cm.get_message_text(MSG_NEW_BLOCK, json.dumps(new_block.to_dict()))
            self.cm.send_msg_to_all_peer(message_new_block)
            index = len(result)
            self.tp.clear_my_transactions(index)
            break

        print('Current Blockchain is ... ', self.bm.chain)
        print('Current prev_block_hash is ... ', self.prev_block_hash)
        self.flag_stop_block_build = False
        self.is_bb_running = False
        self.bb_timer = threading.Timer(CHECK_INTERVAL, self.__generate_block_with_tp)
        self.bb_timer.start()

    def __handle_message(self, msg, is_core, peer=None):
    
        if peer != None:
            if msg[2] == MSG_REQUEST_FULL_CHAIN:
                print('Send our latest blockchain for reply to : ', peer)
                mychain = self.bm.get_my_blockchain()
                print(mychain)
                chain_data = pickle.dumps(mychain, 0).decode()
                new_message = self.cm.get_message_text(RSP_FULL_CHAIN, chain_data)
                self.cm.send_msg(peer, new_message)
        else:
            if msg[2] == MSG_NEW_TRANSACTION:
                # 新規transactionを登録する処理を呼び出す
                new_transaction = json.loads(msg[4])
                print("received new_transaction", new_transaction)
                current_transactions = self.tp.get_stored_transactions()
                bc_transactions = self.bm.get_stored_transactions_from_bc()

                if new_transaction in current_transactions:
                    print("this is already pooled transaction:", new_transaction)
                    return

                else:
                    # 車両から送られてきた位置情報を検知する
                    if len(current_transactions) > 0 and self.vl.verify_trlocinfo(new_transaction, current_transactions):
                        self.c_cnt += 1
                        with open('/home/ryoichi/Templates/BlockchainProjects/result/result_c_cnt.txt', 'a') as f:
                            f.write(str(self.my_port) + "," + str(self.c_cnt) + "\n")
                        print("C_Counter", self.c_cnt)
                        self.tp.set_new_transaction(new_transaction)

                        if not is_core:
                            m_type = MSG_NEW_TRANSACTION
                            new_message = self.cm.get_message_text(m_type, json.dumps(new_transaction))
                            self.cm.send_msg_to_all_peer(new_message)

                    elif len(bc_transactions) > 0 and self.vl.verify_trlocinfo(new_transaction, bc_transactions):
                        self.bc_cnt += 1
                        with open('/home/ryoichi/Templates/BlockchainProjects/result/result_bc_cnt.txt', 'a') as f:
                            f.write(str(self.my_port) + "," + str(self.bc_cnt) + "\n")
                        print("BC_Counter", self.bc_cnt)
                        self.tp.set_new_transaction(new_transaction)

                        if not is_core:
                            m_type = MSG_NEW_TRANSACTION
                            new_message = self.cm.get_message_text(m_type, json.dumps(new_transaction))
                            self.cm.send_msg_to_all_peer(new_message)

                    else:
                        self.false_cnt += 1
                        with open('/home/ryoichi/Templates/BlockchainProjects/result/result_false_cnt.txt', 'a') as f:
                            f.write(str(self.my_port) + "," + str(self.false_cnt) + "\n")
                        print("FALSE_Counter", self.false_cnt)
                        ntr = new_transaction['vehicleinfo']
                        process_time = time.time() - new_transaction['sendtime']
                        ntr_id = [i['ID'] for i in ntr]
                        ntr_ID = ntr_id[0] if len(ntr_id) else ''
                        ntr_ts = [i['timestamp'] for i in ntr]
                        ntr_timestamp = ntr_ts[0] if len(ntr_ts) else ''
                        ntr_or = [i['TorF'] for i in ntr]
                        ntr_torf = ntr_or[0] if len(ntr_or) else ''
                        with open('/home/ryoichi/Templates/BlockchainProjects/result/result_new_transaction.txt', 'a') as f:
                            f.write(str(self.my_port) + "," + str(ntr_ID) + "," + str(ntr_timestamp) +
                                    "," + str(ntr_torf) + "," + str(process_time) + "\n")
                        print("FALSE_Counter", self.false_cnt)
                        self.tp.set_new_transaction(new_transaction)

                        if not is_core:
                            m_type = MSG_NEW_TRANSACTION
                            new_message = self.cm.get_message_text(m_type, json.dumps(new_transaction))
                            self.cm.send_msg_to_all_peer(new_message)

            elif msg[2] == MSG_NEW_BLOCK:

                if not is_core:
                    print('block received from unknown')
                    return

                # 新規ブロックを検証し、正当なものであればブロックチェーンに追加する
                new_block = json.loads(msg[4])
                block_processtime = time.time() - self.vl.extract_newblockprocesstime(new_block)
                with open('/home/ryoichi/Templates/BlockchainProjects/result/result_new_block_time.txt', 'a') as f:
                    f.write(str(self.my_port) + "," + str(block_processtime) + "\n")
                with open('/home/ryoichi/Templates/BlockchainProjects/result/result_new_block.txt', 'a') as f:
                    f.write(str(self.my_port) + "," + str(new_block) + "\n")
                print('new_block: ', new_block)

                if self.bm.is_valid_block(self.prev_block_hash, new_block):
                    # ブロック生成が行われていたら一旦停止してあげる（threadingなのでキレイに止まらない場合あり）
                    if self.is_bb_running:
                        self.flag_stop_block_build = True 
                    self.prev_block_hash = self.bm.get_hash(new_block)
                    self.bm.set_new_block(new_block)
                else:
                    #　ブロックとして不正ではないがVerifyにコケる場合は自分がorphanブロックを生成している
                    #　可能性がある
                    self.get_all_chains_for_resolve_conflict()

            elif msg[2] == RSP_FULL_CHAIN:

                if not is_core:
                    print('blockchain received from unknown')
                    return
                # ブロックチェーン送信要求に応じて返却されたブロックチェーンを検証し、有効なものか検証した上で
                # 自分の持つチェインと比較し優位な方を今後のブロックチェーンとして有効化する
                new_block_chain = pickle.loads(msg[4].encode('utf8'))
                print(new_block_chain)
                result, pool_4_orphan_blocks = self.bm.resolve_conflicts(new_block_chain)
                print('blockchain received')
                if result is not None:
                    self.prev_block_hash = result
                    if len(pool_4_orphan_blocks) != 0:
                        # orphanブロック群の中にあった未処理扱いになるTransactionをTransactionPoolに戻す
                        new_transactions = self.bm.get_transactions_from_orphan_blocks(pool_4_orphan_blocks)
                        for t in new_transactions:
                            self.tp.set_new_transaction(t)
                else:
                    print('Received blockchain is useless...')
            elif msg[2] == MSG_ENHANCED:
                # P2P Network を単なるトランスポートして使っているアプリケーションが独自拡張したメッセージはここで処理する。SimpleBitcoin としてはこの種別は使わない
                self.mpmh.handle_message(msg[4])

    def __get_myip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]

    def get_count(self):
        a = self.c_cnt
        b = self.bc_cnt
        c = self.false_cnt
        return a, b, c
