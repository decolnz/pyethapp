import monkeypatches
import json
import os
import signal
import sys
from uuid import uuid4
import click
from click import BadParameter
import gevent
from gevent.event import Event
import rlp
from devp2p.service import BaseService
from devp2p.peermanager import PeerManager
from devp2p.discovery import NodeDiscovery
from devp2p.app import BaseApp
import eth_protocol
from eth_service import ChainService
from console_service import Console
from ethereum.blocks import Block
import ethereum.slogging as slogging
import config as konfig
from db_service import DBService
from jsonrpc import JSONRPCServer
from pow_service import PoWService
from accounts import AccountsService, Account
from pyethapp import __version__
import utils

slogging.configure(config_string=':debug')
log = slogging.get_logger('app')


services = [DBService, AccountsService, NodeDiscovery, PeerManager, ChainService, PoWService,
            JSONRPCServer, Console]


class EthApp(BaseApp):
    client_version = 'pyethapp/v%s/%s/%s' % (__version__, sys.platform,
                                             'py%d.%d.%d' % sys.version_info[:3])
    default_config = dict(BaseApp.default_config)
    default_config['client_version'] = client_version
    default_config['post_app_start_callback'] = None


@click.group(help='Welcome to ethapp version:{}'.format(EthApp.client_version))
@click.option('alt_config', '--Config', '-C', type=click.File(), help='Alternative config file')
@click.option('config_values', '-c', multiple=True, type=str,
              help='Single configuration parameters (<param>=<value>)')
@click.option('data_dir', '--data-dir', '-d', multiple=False, type=str,
              help='data directory')
@click.option('log_config', '--log_config', '-l', multiple=False, type=str,
              help='log_config string: e.g. ":info,eth:debug')
@click.option('--log-json/--log-no-json', default=False,
              help='log as structured json output')
@click.option('bootstrap_node', '--bootstrap_node', '-b', multiple=False, type=str,
              help='single bootstrap_node as enode://pubkey@host:port')
@click.option('mining_pct', '--mining_pct', '-m', multiple=False, type=int, default=0,
              help='pct cpu used for mining')
@click.option('--unlock', multiple=True, type=str,
              help='Unlock an account (prompts for password)')
@click.option('--password', type=click.File(), help='path to a password file')
@click.pass_context
def app(ctx, alt_config, config_values, data_dir, log_config, bootstrap_node, log_json,
        mining_pct, unlock, password):

    # configure logging
    log_config = log_config or ':info'
    slogging.configure(log_config, log_json=log_json)

    # data dir default or from cli option
    data_dir = data_dir or konfig.default_data_dir
    konfig.setup_data_dir(data_dir)  # if not available, sets up data_dir and required config
    log.info('using data in', path=data_dir)

    # prepare configuration
    # config files only contain required config (privkeys) and config different from the default
    if alt_config:  # specified config file
        config = konfig.load_config(alt_config)
    else:  # load config from default or set data_dir
        config = konfig.load_config(data_dir)

    config['data_dir'] = data_dir

    # add default config
    konfig.update_config_with_defaults(config, konfig.get_default_config([EthApp] + services))

    # override values with values from cmd line
    for config_value in config_values:
        try:
            konfig.set_config_param(config, config_value)
            # check if this is part of the default config
        except ValueError:
            raise BadParameter('Config parameter must be of the form "a.b.c=d" where "a.b.c" '
                               'specifies the parameter to set and d is a valid yaml value '
                               '(example: "-c jsonrpc.port=5000")')
    if bootstrap_node:
        config['discovery']['bootstrap_nodes'] = [bytes(bootstrap_node)]
    if mining_pct > 0:
        config['pow']['activated'] = True
        config['pow']['cpu_pct'] = int(min(100, mining_pct))
    if not config['pow']['activated']:
        config['deactivated_services'].append(PoWService.name)

    ctx.obj = {'config': config,
               'unlock': unlock,
               'password': password.read().rstrip() if password else None}


@app.command()
@click.option('--dev/--nodev', default=False, help='Exit at unhandled exceptions')
@click.option('--nodial/--dial',  default=False, help='do not dial nodes')
@click.option('--fake/--nofake',  default=False, help='fake genesis difficulty')
@click.pass_context
def run(ctx, dev, nodial, fake):
    """Start the client ( --dev to stop on error)"""
    config = ctx.obj['config']
    if nodial:
        # config['deactivated_services'].append(PeerManager.name)
        # config['deactivated_services'].append(NodeDiscovery.name)
        config['discovery']['bootstrap_nodes'] = []
        config['discovery']['listen_port'] = 29873
        config['p2p']['listen_port'] = 29873
        config['p2p']['min_peers'] = 0

    if fake:
        from ethereum import blocks
        blocks.GENESIS_DIFFICULTY = 1024
        blocks.BLOCK_DIFF_FACTOR = 16
    # create app
    app = EthApp(config)

    # development mode
    if dev:
        gevent.get_hub().SYSTEM_ERROR = BaseException
        try:
            config['client_version'] += '/' + os.getlogin()
        except:
            log.warn("can't get and add login name to client_version")
            pass

    # dump config
    konfig.dump_config(config)

    # register services
    for service in services:
        assert issubclass(service, BaseService)
        if service.name not in app.config['deactivated_services']:
            assert service.name not in app.services
            service.register_with_app(app)
            assert hasattr(app.services, service.name)

    # start app
    log.info('starting')
    app.start()

    if config['post_app_start_callback'] is not None:
        config['post_app_start_callback'](app)

    # wait for interrupt
    evt = Event()
    gevent.signal(signal.SIGQUIT, evt.set)
    gevent.signal(signal.SIGTERM, evt.set)
    gevent.signal(signal.SIGINT, evt.set)
    evt.wait()

    # finally stop
    app.stop()


@app.command()
@click.pass_context
def config(ctx):
    """Show the config"""
    konfig.dump_config(ctx.obj['config'])


@app.command()
@click.argument('file', type=click.File(), required=True)
@click.argument('name', type=str, required=True)
@click.pass_context
def blocktest(ctx, file, name):
    """Start after importing blocks from a file.

    In order to prevent replacement of the local test chain by the main chain from the network, the
    peermanager, if registered, is stopped before importing any blocks.

    Also, for block tests an in memory database is used. Thus, a already persisting chain stays in
    place.
    """
    app = EthApp(ctx.obj['config'])
    app.config['db']['implementation'] = 'EphemDB'

    # register services
    for service in services:
        assert issubclass(service, BaseService)
        if service.name not in app.config['deactivated_services']:
            assert service.name not in app.services
            service.register_with_app(app)
            assert hasattr(app.services, service.name)

    if ChainService.name not in app.services:
        log.fatal('No chainmanager registered')
        ctx.abort()
    if DBService.name not in app.services:
        log.fatal('No db registered')
        ctx.abort()

    log.info('loading block file', path=file.name)
    try:
        data = json.load(file)
    except ValueError:
        log.fatal('Invalid JSON file')
    if name not in data:
        log.fatal('Name not found in file')
        ctx.abort()
    try:
        blocks = utils.load_block_tests(data.values()[0], app.services.chain.chain.db)
    except ValueError:
        log.fatal('Invalid blocks encountered')
        ctx.abort()

    # start app
    app.start()
    if 'peermanager' in app.services:
        app.services.peermanager.stop()

    log.info('building blockchain')
    Block.is_genesis = lambda self: self.number == 0
    app.services.chain.chain._initialize_blockchain(genesis=blocks[0])
    for block in blocks[1:]:
        app.services.chain.chain.add_block(block)

    # wait for interrupt
    evt = Event()
    gevent.signal(signal.SIGQUIT, evt.set)
    gevent.signal(signal.SIGTERM, evt.set)
    gevent.signal(signal.SIGINT, evt.set)
    evt.wait()

    # finally stop
    app.stop()


@app.command('export')
@click.option('--from', 'from_', type=int, help='Number of the first block (default: genesis)')
@click.option('--to', type=int, help='Number of the last block (default: latest)')
@click.argument('file', type=click.File('ab'))
@click.pass_context
def export_blocks(ctx, from_, to, file):
    """Export the blockchain to <FILE>.

    The chain will be stored in binary format, i.e. as a concatenated list of RLP encoded blocks,
    starting with the earliest block.

    If the file already exists, the additional blocks are appended. Otherwise, a new file is
    created.

    Use - to write to stdout.
    """
    app = EthApp(ctx.obj['config'])
    DBService.register_with_app(app)
    AccountsService.register_with_app(app)
    ChainService.register_with_app(app)

    if from_ is None:
        from_ = 0
    head_number = app.services.chain.chain.head.number
    if to is None:
        to = head_number
    if from_ < 0:
        log.fatal('block numbers must not be negative')
        sys.exit(1)
    if to < from_:
        log.fatal('"to" block must be newer than "from" block')
        sys.exit(1)
    if to > head_number:
        log.fatal('"to" block not known (current head: {})'.format(head_number))
        sys.exit(1)

    log.info('Starting export')
    for n in xrange(from_, to + 1):
        log.debug('Exporting block {}'.format(n))
        if (n - from_) % 50000 == 0:
            log.info('Exporting block {} to {}'.format(n, min(n + 50000, to)))
        block_hash = app.services.chain.chain.index.get_block_by_number(n)
        # bypass slow block decoding by directly accessing db
        block_rlp = app.services.db.get(block_hash)
        file.write(block_rlp)
    log.info('Export complete')


@app.command('import')
@click.argument('file', type=click.File('rb'))
@click.pass_context
def import_blocks(ctx, file):
    """Import blocks from <FILE>.

    Blocks are expected to be in binary format, i.e. as a concatenated list of RLP encoded blocks.

    Blocks are imported sequentially. If a block can not be imported (e.g. because it is badly
    encoded, it is in the chain already or its parent is not in the chain) it will be ignored, but
    the process will continue. Sole exception: If neither the first block nor its parent is known,
    importing will end right away.

    Use - to read from stdin.
    """
    app = EthApp(ctx.obj['config'])
    DBService.register_with_app(app)
    AccountsService.register_with_app(app)
    ChainService.register_with_app(app)
    chain = app.services.chain
    assert chain.block_queue.empty()

    data = file.read()
    app.start()

    def blocks():
        """Generator for blocks encoded in `data`."""
        i = 0
        while i < len(data):
            try:
                block_data, next_i = rlp.codec.consume_item(data, i)
            except rlp.DecodingError:
                log.fatal('invalid RLP encoding', byte_index=i)
                sys.exit(1)  # have to abort as we don't know where to continue
            try:
                if not isinstance(block_data, list) or len(block_data) != 3:
                    raise rlp.DeserializationError('', block_data)
                yield eth_protocol.TransientBlock(block_data)
            except (IndexError, rlp.DeserializationError):
                log.warning('not a valid block', byte_index=i)  # we can still continue
                yield None
            i = next_i

    log.info('importing blocks')
    # check if it makes sense to go through all blocks
    first_block = next(blocks())
    if first_block is None:
        log.fatal('first block invalid')
        sys.exit(1)
    if not (chain.knows_block(first_block.header.hash) or
            chain.knows_block(first_block.header.prevhash)):
        log.fatal('unlinked chains', newest_known_block=chain.chain.head.number,
                  first_unknown_block=first_block.header.number)
        sys.exit(1)

    # import all blocks
    for n, block in enumerate(blocks()):
        if block is None:
            log.warning('skipping block', number_in_file=n)
            continue
        log.debug('adding block to queue', number_in_file=n, number_in_chain=block.header.number)
        app.services.chain.add_block(block, None)  # None for proto

    # let block processing finish
    while not app.services.chain.block_queue.empty():
        gevent.sleep()
    app.stop()
    log.info('import finished', head_number=app.services.chain.chain.head.number)


@app.group()
@click.pass_context
def account(ctx):
    """Manage accounts.

    For accounts to be accessible by pyethapp, their keys must be stored in the keystore directory.
    Its path can be configured through "accounts.keystore_dir".
    """
    app = EthApp(ctx.obj['config'])
    ctx.obj['app'] = app
    AccountsService.register_with_app(app)
    unlock_accounts(ctx.obj['unlock'], app.services.accounts, password=ctx.obj['password'])


@account.command('new')
@click.option('--uuid', '-i', help='equip the account with a random UUID', is_flag=True)
@click.pass_context
def new_account(ctx, uuid):
    """Create a new account.

    This will generate a random private key and store it in encrypted form in the keystore
    directory. You are prompted for the password that is employed (if no password file is
    specified). If desired the private key can be associated with a random UUID (version 4) using
    the --uuid flag.
    """
    app = ctx.obj['app']
    if uuid:
        id_ = str(uuid4())
    else:
        id_ = None
    password = ctx.obj['password']
    if password is None:
        password = click.prompt('Password to encrypt private key', default='', hide_input=True,
                                confirmation_prompt=True, show_default=False)
    account = Account.new(password, uuid=id_)
    try:
        app.services.accounts.add_account(account, path=account.address.encode('hex'))
    except IOError:
        click.echo('Could not write keystore file. Make sure you have write permission in the '
                   'configured directory and check the log for further information.')
        sys.exit(1)
    else:
        click.echo('Account creation successful')
        click.echo('  Address: ' + account.address.encode('hex'))
        click.echo('       Id: ' + str(account.uuid))


@account.command('list')
@click.pass_context
def list_accounts(ctx):
    """List accounts with addresses and ids.

    This prints a table of all accounts, numbered consecutively, along with their addresses and
    ids. Note that some accounts do not have an id, and some addresses might be hidden (i.e. are
    not present in the keystore file). In the latter case, you have to unlock the accounts (e.g.
    via "pyethapp --unlock <account> account list") to display the address anyway.
    """
    accounts = ctx.obj['app'].services.accounts
    if len(accounts) == 0:
        click.echo('no accounts found')
    else:
        fmt = '{i:>4} {address:<40} {id:<36} {locked:<1}'
        click.echo('     {address:<40} {id:<36} {locked}'.format(address='Address (if known)',
                                                                 id='Id (if any)',
                                                                 locked='Locked'))
        for i, account in enumerate(accounts):
            click.echo(fmt.format(i='#' + str(i + 1),
                                  address=(account.address or '').encode('hex'),
                                  id=account.uuid or '',
                                  locked='yes' if account.locked else 'no'))


@account.command('import')
@click.argument('f', type=click.File(), metavar='FILE')
@click.option('--uuid', '-i', help='equip the new account with a random UUID', is_flag=True)
@click.pass_context
def import_accounts(ctx, f, uuid):
    """Import a private key from FILE.

    FILE is the path to the file in which the private key is stored. The key is assumed to be hex
    encoded, surrounding whitespace is stripped. A new account is created for the private key, as
    if it was created with "pyethapp account new", and stored in the keystore directory. You will
    be prompted for a password to encrypt the key (if no password file is specified). If desired a
    random UUID (version 4) can be generated using the --uuid flag in order to identify the new
    account later.
    """
    app = ctx.obj['app']
    if uuid:
        id_ = str(uuid4())
    else:
        id_ = None
    privkey_hex = f.read()
    try:
        privkey = privkey_hex.strip().decode('hex')
    except TypeError:
        click.echo('Could not decode private key from file (should be hex encoded)')
        sys.exit(1)
    password = ctx.obj['password']
    if password is None:
        password = click.prompt('Password to encrypt private key', default='', hide_input=True,
                                confirmation_prompt=True, show_default=False)
    account = Account.new(password, privkey, uuid=id_)
    try:
        app.services.accounts.add_account(account, path=account.address.encode('hex'))
    except IOError:
        click.echo('Could not write keystore file. Make sure you have write permission in the '
                   'configured directory and check the log for further information.')
        sys.exit(1)
    else:
        click.echo('Account creation successful')
        click.echo('  Address: ' + account.address.encode('hex'))
        click.echo('       Id: ' + str(account.uuid))


def unlock_accounts(account_ids, account_service, max_attempts=3, password=None):
    """Unlock a list of accounts., prompting for passwords one by one if not given.

    If a password is specified, it will be used to unlock all accounts. If not, the user is
    prompted for one password per account.

    If an account can not be identified or unlocked, an error message is logged and the program
    exits.

    :param accounts: a list of account identifiers accepted by :meth:`AccountsService.find`
    :param account_service: the account service managing the given accounts
    :param max_attempts: maximum number of attempts per account before the unlocking process is
                         aborted (>= 1), or `None` to allow an arbitrary number of tries
    :param password: optional password which will be used to unlock the accounts
    """
    accounts = []
    for account_id in account_ids:
        try:
            account = account_service.find(account_id)
        except KeyError:
            log.fatal('could not find account', identifier=account_id)
            sys.exit(1)
        accounts.append(account)

    if password is not None:
        for identifier, account in zip(account_ids, accounts):
            try:
                account.unlock(password)
            except ValueError:
                log.fatal('Could not unlock account with password from file',
                          account_id=identifier)
        return

    max_attempts_str = str(max_attempts) if max_attempts else 'oo'
    attempt_fmt = '(attempt {{attempt}}/{})'.format(max_attempts_str)
    first_attempt_fmt = 'Password for account {id} ' + attempt_fmt
    further_attempts_fmt = 'Wrong password. Please try again ' + attempt_fmt

    for identifier, account in zip(account_ids, accounts):
        attempt = 1
        pw = click.prompt(first_attempt_fmt.format(id=identifier, attempt=1), hide_input=True,
                          default='', show_default=False)
        while True:
            attempt += 1
            try:
                account.unlock(pw)
            except ValueError:
                if max_attempts and attempt > max_attempts:
                    log.fatal('Too many unlock attempts', attempts=attempt, account_id=identifier)
                    sys.exit(1)
                else:
                    pw = click.prompt(further_attempts_fmt.format(attempt=attempt),
                                      hide_input=True, default='', show_default=False)
            else:
                break
        assert not account.locked


if __name__ == '__main__':
    #  python app.py 2>&1 | less +F
    app()
