<?php
declare(strict_types=1);

use Migrations\AbstractMigration;

class CreateArticles extends AbstractMigration
{
    public function change(): void
    {
        $table = $this->table('articles');
        $table->addColumn('user_id', 'integer', ['null' => true])
            ->addColumn('title', 'string', ['limit' => 255])
            ->addColumn('slug', 'string', ['limit' => 255])
            ->addColumn('published', 'boolean', ['default' => false])
            ->addColumn('author_first', 'string', ['limit' => 100, 'null' => true])
            ->addColumn('author_last', 'string', ['limit' => 100, 'null' => true])
            ->addColumn('created', 'datetime', ['null' => true])
            ->addColumn('modified', 'datetime', ['null' => true])
            ->addIndex(['slug'], ['unique' => true])
            ->addForeignKey('user_id', 'users', 'id')
            ->create();
    }
}
