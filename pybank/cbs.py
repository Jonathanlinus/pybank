#!/usr/bin/env python

import socket
import sys
import struct
import sqlite3

from binascii import hexlify

from bpc8583.ISO8583 import ISO8583, MemDump, ParseError
from bpc8583.spec import IsoSpec, IsoSpec1987ASCII, IsoSpec1987BPC
from bpc8583.tools import get_response
from tracetools.tracetools import trace
from pynblock.tools import B2raw

from pybank.db import Database

class CBS:
    def __init__(self, host=None, port=None):
        if host:
            self.host = host
        else:
            self.host = '127.0.0.1'

        if port:
            try:
                self.port = int(port)
            except ValueError:
                print('Invalid TCP port: {}'.format(arg))
                sys.exit()
        else:
            self.port = 3388

        self.db = Database('cbs.db')
        self.responses = {'Approval': '000', 'Invalid account number': '914', 'Insufficient funds': '915', }


    def get_message_length(self, message):
        return B2raw(bytes(str(len(message)).zfill(4), 'utf-8'))


    def get_balance_string(self, balance, currency_code):
        """
        Get balance string, according to Field 54 description
        """
        if not balance or not currency_code:
            return ''
    
        if balance > 0:
            amount_sign = 'C'
        else:
            amount_sign = 'D'
    
        balance_formatted = '{0:.2f}'.format(balance).replace(' ', '').replace('.', '').replace('-', '').zfill(12)
        balance_string = amount_sign + balance_formatted + currency_code
    
        return '007' + str(len(balance_string)).zfill(3) + balance_string

    def get_float_amount(self, amount, currency):
        """
        TODO: check currency exponent, currently using 2 by default
        """
        if amount:
            return amount / 100.0 
        else:
            return .0


    def connect(self):
        """
        """
        try:
            self.sock = None
            for res in socket.getaddrinfo(self.host, self.port, socket.AF_UNSPEC, socket.SOCK_STREAM):
                af, socktype, proto, canonname, sa = res
                self.sock = socket.socket(af, socktype, proto)
                self.sock.connect(sa)
        except OSError as msg:
            print('Error connecting to {}:{} - {}'.format(self.host, self.port, msg))
            sys.exit()
        print('Connected to {}:{}'.format(self.host, self.port))


    def process_trxn_balance_inquiry(self, request, response):
        card_number = request.FieldData(2)
        currency_code = request.FieldData(51)
        if not currency_code:
            currency_code = request.FieldData(49)

        available_balance = self.db.get_card_balance(card_number, currency_code)

        response.FieldData(54, self.get_balance_string(available_balance, currency_code))
        response.FieldData(39, '00')


    def process_trxn_debit_account(self, request, response):
        """
        """
        card_number = request.FieldData(2)
        currency_code = request.FieldData(51)
        amount_cardholder_billing = self.get_float_amount(request.FieldData(6), currency_code)

        available_balance = self.db.get_card_balance(card_number, currency_code)

        if available_balance:
            if available_balance > amount_cardholder_billing:
                self.db.update_card_balance(card_number, currency_code, available_balance - amount_cardholder_billing)
                response.FieldData(39, self.responses['Approval'])

                available_balance = self.db.get_card_balance(card_number, currency_code)
                response.FieldData(54, self.get_balance_string(available_balance, currency_code))
            else:
                response.FieldData(39, self.responses['Insufficient funds'])
        else:
            response.FieldData(39, self.responses['Invalid account number'])


    def settle_auth_advice(self, request, response):
        """
        """
        card_number = request.FieldData(2)
        currency_code = request.FieldData(51)
        amount_cardholder_billing = self.get_float_amount(request.FieldData(6), currency_code)

        available_balance = self.db.get_card_balance(card_number, currency_code)
        self.db.update_card_balance(card_number, currency_code, available_balance - amount_cardholder_billing)
        response.FieldData(39, self.responses['Approval'])


    def settle_reversal(self, request, response):
        """
        """
        card_number = request.FieldData(2)
        currency_code = request.FieldData(51)
        amount_cardholder_billing = self.get_float_amount(request.FieldData(6), currency_code)

        available_balance = self.db.get_card_balance(card_number, currency_code)
        self.db.update_card_balance(card_number, currency_code, available_balance + amount_cardholder_billing)
        response.FieldData(39, self.responses['Approval'])


    def init_response_message(self, request):
        """
        """
        response = ISO8583(None, IsoSpec1987BPC())
        response.MTI(get_response(request.get_MTI()))

        # Copy some key fields from original message:
        for field in [2, 3, 4, 5, 6, 11, 12, 14, 15, 17, 24, 32, 37, 48, 49, 50, 51, 102]:
            response.FieldData(field, request.FieldData(field))

        return response


    def get_transaction_type(self, request):
        """
        """
        if request.FieldData(3) != None:
            processing_code = str(request.FieldData(3)).zfill(6)
            return processing_code[0:2]
        else:
            return None


    def run(self):
        """
        """
        while True:
            try:
                self.connect()

                while True:
                    data = self.sock.recv(4096)
                    if len(data) > 0:
                        trace(title='<< {} bytes received: '.format(len(data)), data=data)
                    
                    request = ISO8583(data[2:], IsoSpec1987BPC())
                    request.Print()

                    response = self.init_response_message(request)

                    MTI = str(request.get_MTI()).zfill(4)[-3:]
                    trxn_type = self.get_transaction_type(request)

                    if MTI in ['100', '200']:
                        # Authorization request or financial request
                        if trxn_type == '31':
                            # Balance
                            self.process_trxn_balance_inquiry(request, response)
                        elif trxn_type in ['00', '01']:
                            # Purchase or ATM Cash
                            self.process_trxn_debit_account(request, response)
                        else:
                            response.FieldData(39, self.responses['Approval'])

                    elif MTI in ['120']:
                        # Authorization advice
                        if trxn_type in ['00', '01']:
                            # Purchase or ATM Cash
                            self.settle_auth_advice(request, response)
                        else:
                            response.FieldData(39, self.responses['Approval'])

                    elif MTI in ['400', '420']:
                        # Reversal
                        if trxn_type in ['00', '01']:
                            # Purchase or ATM Cash
                            self.settle_reversal(request, response)

                    response.Print()
                    
                    data = response.BuildIso()
                    data = self.get_message_length(data) + data
                    self.sock.send(data)
                    trace(title='>> {} bytes sent:'.format(len(data)), data=data)
        
            except ParseError:
                print('Connection closed')
                conn.close()

        self.sock.close()
        conn.close()
