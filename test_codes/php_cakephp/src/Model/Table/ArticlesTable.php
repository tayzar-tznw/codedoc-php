<?php
declare(strict_types=1);

namespace App\Model\Table;

use ArrayObject;
use Cake\Datasource\EntityInterface;
use Cake\Event\EventInterface;
use Cake\ORM\Query\SelectQuery;
use Cake\ORM\Table;

class ArticlesTable extends Table
{
    public function initialize(array $config): void
    {
        parent::initialize($config);

        $this->setTable('articles');
        $this->addBehavior('Timestamp');
        $this->addBehavior('Billing.Audit');
        $this->belongsTo('Users');
    }

    public function findRecent(SelectQuery $query): SelectQuery
    {
        return $query->orderBy(['created' => 'DESC'])->limit(10);
    }

    public function beforeSave(EventInterface $event, EntityInterface $entity, ArrayObject $options): void
    {
        if ($entity->get('slug') === null) {
            $entity->set('slug', strtolower(str_replace(' ', '-', (string)$entity->get('title'))));
        }
    }
}
