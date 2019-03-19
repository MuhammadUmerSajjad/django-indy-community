from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.views.generic.edit import UpdateView

from django.conf import settings

from time import sleep

from ..models import *
from ..utils import *
from ..wallet_utils import *
from ..registration_utils import *
from ..agent_utils import *


class AgentInteractionTests(TestCase):

    def create_user_and_org(self):
        # create, register and provision a user and org
        # create, register and provision a user
        email = random_alpha_string(10) + "@agent_utils.com"
        user_wallet_name = get_user_wallet_name(email)
        user = get_user_model().objects.create(
            email=email,
            first_name='Test',
            last_name='Registration',
        )
        user.save()
        raw_password = random_alpha_string(8)
        user_provision(user, raw_password)

        # now org
        org_name = 'Agent Utils ' + random_alpha_string(10)
        org = org_signup(user, raw_password, org_name)

        return (user, org, raw_password)

    def establish_agent_connection(self, org, user):
        # send connection request (org -> user)
        org_connection_1 = send_connection_invitation(org.wallet, user.email)
        sleep(1)

        # accept connection request (user -> org)
        user_connection = send_connection_confirmation(user.wallet, org.org_name, org_connection_1.invitation)
        sleep(1)

        # update connection status (org)
        org_connection_2 = check_connection_status(org.wallet, org_connection_1)

        return (org_connection_2, user_connection)

    def delete_user_and_org_wallets(self, user, org, raw_password):
        # cleanup after ourselves
        org_wallet_name = org.wallet.wallet_name
        res = delete_wallet(org_wallet_name, raw_password)
        self.assertEqual(res, 0)
        user_wallet_name = user.wallet.wallet_name
        res = delete_wallet(user_wallet_name, raw_password)
        self.assertEqual(res, 0)

    def schema_and_cred_def_for_org(self, org):
        # create a "dummy" schema/cred-def that is unique to this org (matches the Alice/Faber demo schema)
        wallet = org.wallet
        wallet_name = org.wallet.wallet_name

        (schema_json, creddef_template) = create_schema_json('schema_' + wallet_name, random_schema_version(), [
            'name', 'date', 'degree', 'age',
            ])
        schema = create_schema(wallet, schema_json, creddef_template)
        creddef = create_creddef(wallet, schema, 'creddef_' + wallet_name, creddef_template)

         # Proof of Age
        proof_request = create_proof_request('Proof of Age Test', 'Proof of Age Test',
            [{'name':'name', 'restrictions':[{'issuer_did': '$ISSUER_DID'}]}],
            [{'name': 'age','p_type': '>=','p_value': '$VALUE', 'restrictions':[{'issuer_did': '$ISSUER_DID'}]}]
            )

        return (schema, creddef, proof_request)


    def test_register_org_with_schema_and_cred_def(self):
        # try creating a schema and credential definition under the organization
        (user, org, raw_password) = self.create_user_and_org()
        (schema, creddef, proof_request) = self.schema_and_cred_def_for_org(org)

        # fetch some stuff and validate some other stuff
        fetch_org = IndyOrganization.objects.filter(org_name=org.org_name).all()[0]
        self.assertEqual(len(fetch_org.wallet.indycreddef_set.all()), 1)
        fetch_creddef = fetch_org.wallet.indycreddef_set.all()[0]
        self.assertEqual(fetch_creddef.creddef_name, creddef.creddef_name)

        # clean up after ourself
        self.delete_user_and_org_wallets(user, org, raw_password)


    def test_agent_connection(self):
        # establish a connection between two agents
        (user, org, raw_password) = self.create_user_and_org()
        (schema, creddef, proof_request) = self.schema_and_cred_def_for_org(org)

        (org_connection, user_connection) = self.establish_agent_connection(org, user)
        self.assertEqual(org_connection.status, 'Active')

        # clean up after ourself
        self.delete_user_and_org_wallets(user, org, raw_password)


    def test_agent_credential_exchange(self):
        # exchange credentials between two agents
        (user, org, raw_password) = self.create_user_and_org()
        (schema, creddef, proof_request) = self.schema_and_cred_def_for_org(org)

        # establish a connection
        (org_connection, user_connection) = self.establish_agent_connection(org, user)

        # issue credential offer (org -> user)
        cred_def = org.wallet.indycreddef_set.all()[0]
        schema_attrs = json.loads(cred_def.creddef_template)
        # data normally provided by the org data pipeline
        schema_attrs['name'] = 'Joe Smith'
        schema_attrs['date'] = '2018-01-01'
        schema_attrs['degree'] = 'B.A.Sc. Honours'
        schema_attrs['age'] = '25'
        org_conversation_1 = send_credential_offer(org.wallet, org_connection,  
                                            'Some Tag Value', schema_attrs, cred_def, 
                                            'Some Credential Name')
        sleep(2)

        # poll to receive credential offer
        user_conversations = AgentConversation.objects.filter(wallet=user.wallet, conversation_type="CredentialOffer", status='Pending').all()
        self.assertEqual(len(user_conversations), 0)
        user_credentials = list_wallet_credentials(user.wallet)
        self.assertEqual(len(user_credentials), 0)

        i = 0
        while True:
            handled_count = handle_inbound_messages(user.wallet, user_connection)
            i = i + 1
            if handled_count > 0 or i > 3:
                break
            sleep(2)
        self.assertEqual(handled_count, 1)
        user_conversations = AgentConversation.objects.filter(wallet=user.wallet, conversation_type="CredentialOffer", status='Pending').all()
        self.assertEqual(len(user_conversations), 1)
        user_conversation_1 = user_conversations[0]

        # send credential request (user -> org)
        user_conversation_2 = send_credential_request(user.wallet, user_connection, user_conversation_1)
        sleep(2)

        # send credential (org -> user)
        i = 0
        message = org_conversation_1
        while True:
            message = poll_message_conversation(org.wallet, org_connection, message, initialize_vcx=True)
            i = i + 1
            if message.conversation_type == 'IssueCredential' or i > 3:
                break
            sleep(2)
        self.assertEqual(message.conversation_type, 'IssueCredential')
        org_conversation_2 = message
        sleep(2)

        # accept credential and update status (user)
        i = 0
        message = user_conversation_2
        while True:
            message = poll_message_conversation(user.wallet, user_connection, message, initialize_vcx=True)
            i = i + 1
            if message.status == 'Accepted' or i > 3:
                break
            sleep(2)
        self.assertEqual(message.status, 'Accepted')
        user_conversation_3 = message
        sleep(2)

        # update credential offer status (org)
        i = 0
        message = org_conversation_2
        while True:
            message = poll_message_conversation(org.wallet, org_connection, message, initialize_vcx=True)
            i = i + 1
            if message.status == 'Accepted' or i > 3:
                break
            sleep(2)
        self.assertEqual(message.status, 'Accepted')
        org_conversation_3 = message

        # verify credential is in user wallet
        user_credentials = list_wallet_credentials(user.wallet)
        self.assertEqual(len(user_credentials), 1)

        # clean up after ourself
        self.delete_user_and_org_wallets(user, org, raw_password)


    def test_agent_proof_exchange(self):
        # request and deliver a proof between two agents
        (user, org, raw_password) = self.create_user_and_org()
        (schema, creddef, proof_request) = self.schema_and_cred_def_for_org(org)

        # TODO issue credential (org -> user)

        # TODO issue proof request (org -> user)

        # TODO accept proof request (user)

        # TODO select credential(s) for proof (user)

        # TODO construct proof and send (user -> org)

        # TODO accept and validate proof (org)

        # clean up after ourself
        self.delete_user_and_org_wallets(user, org, raw_password)
