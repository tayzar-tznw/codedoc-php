<?php
declare(strict_types=1);

namespace App\Command;

use App\Service\InvoiceMailer;
use Cake\Command\Command;
use Cake\Console\Arguments;
use Cake\Console\ConsoleIo;
use Cake\Core\Configure;

class SendInvoicesCommand extends Command
{
    public function __construct(private InvoiceMailer $mailer)
    {
        parent::__construct();
    }

    public function execute(Arguments $args, ConsoleIo $io): int
    {
        $appName = (string)Configure::read('App.name');
        $result = $this->mailer->deliver((int)$args->getArgument('id'));
        $io->out($appName . ': ' . $result);

        return static::CODE_SUCCESS;
    }
}
