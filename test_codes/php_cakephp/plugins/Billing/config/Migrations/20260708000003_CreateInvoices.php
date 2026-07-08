<?php
declare(strict_types=1);

use Migrations\AbstractMigration;

class CreateInvoices extends AbstractMigration
{
    public function change(): void
    {
        $table = $this->table('invoices');
        $table->addColumn('total', 'decimal', ['precision' => 10, 'scale' => 2])
            ->addColumn('synced', 'boolean', ['default' => false])
            ->addColumn('created', 'datetime', ['null' => true])
            ->create();
    }
}
